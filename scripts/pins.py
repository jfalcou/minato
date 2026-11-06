#!/usr/bin/env python3
"""Measure the pins of the libraries on each other, and move them to the newest versions.

Usage:
    pins.py scan <clone> [--library <name>]
        The pins of one clone, as JSON, under the holds of the library when one is named.
    pins.py check --clones <dir> [--write] [--diff]
        Every library under libraries/, its clone in <dir>/<name>: a count per status, its
        pins.json written with --write, and the changes a move would make with --diff. The exit
        status is 1 when a library could not be measured.
    pins.py apply <library> <clone> [--summary <file>]
        The pins of one clone moved in place, one line printed per pin moved, and with --summary
        the title and the body of their pull request written as JSON when a pin moves. A library
        whose library.json sets `updates` to false is refused.

A pin names a repository of an owner of minato.json, or a tag of one of its images:
    - the GIT_TAG of a CPMAddPackage, CPMDeclarePackage, or CPMFindPackage call, its VERSION, or
      the `gh:owner/name#ref@version` form of the call,
    - the ref of a `uses:` in a YAML file,
    - a full image reference, such as `ghcr.io/jfalcou/compilers:v11`, in a YAML file or a
      Dockerfile,
    - the `image:` of a matrix file, a tag of the registry minato.json names.

A tag moves to the newest tag of its family, a SHA followed by a tag comment to the commit of the
newest tag of the comment's family, and a bare SHA to the head of the default branch. A VERSION
follows the tag of its call. A branch, a CMake variable, and a tag outside every family stay. A pin
that names a tag the source does not have, or a SHA that is not the commit of its tag, is
reported unknown and left as it is, so no pin ever moves to an older version.
"""

import argparse
import dataclasses
import difflib
import fnmatch
import http
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
TIMEOUT = 60
PAGES = 100

SHA = re.compile(r"[0-9a-f]{40}")
# A repository tag: a prefix, an optional v, and dotted numbers, `4.0`, `v4.0.0`, `hpx-v1.9.0`.
REPO_TAG = re.compile(r"(?P<prefix>(?:[A-Za-z0-9]+[-_])*)(?P<v>v?)(?P<number>\d+(?:\.\d+)*)")
# An image tag: `vN`, or a name and `-vN`, `sycl-v1`. Any other tag of an image is ignored.
IMAGE_TAG = re.compile(r"(?P<prefix>(?:[a-z0-9]+-)?)(?P<v>v)(?P<number>\d+)")
FULL_TAG = re.compile(r"(?:[A-Za-z0-9]+[-_])*v\d+\.\d+\.\d+")
# A comment word read as a tag the author meant, though the source may not have it: `v5.0.0`.
TAG_SHAPED = re.compile(r"(?:[A-Za-z0-9]+[-_])*v\d+(?:\.\d+)*")

CPM_CALL = re.compile(r"\b(?:CPMAddPackage|CPMDeclarePackage|CPMFindPackage)\s*\(", re.IGNORECASE)
CPM_GITHUB = re.compile(r"\bGITHUB_REPOSITORY\s+[\"']?(?P<source>[\w.-]+/[\w.-]+?)[\"']?(?=[\s)]|$)")
CPM_GIT = re.compile(
    r"\bGIT_REPOSITORY\s+[\"']?https://github\.com/(?P<source>[\w.-]+/[\w.-]+?)(?:\.git)?[\"']?(?=[\s)]|$)"
)
CPM_TAG = re.compile(r"\bGIT_TAG\s+(?P<quote>\"?)(?P<ref>[^\s\")]+)(?P=quote)")
CPM_VERSION = re.compile(r"\bVERSION\s+(?P<quote>\"?)(?P<version>\d+(?:\.\d+)*)(?P=quote)(?=[\s)]|$)")
# `gh:owner/name#ref@version`, `gh:owner/name@version#ref`, and the same after `https://github.com/owner/name.git`,
# quoted or not, as CPM reads them.
CPM_URI = re.compile(
    r"""(?<![\w/:])(?P<q>"?)(?:gh:(?P<source>[\w.-]+/[\w.-]+?)|https://github\.com/(?P<url>[\w.-]+/[\w.-]+?)\.git/?)"""
    r"""(?:(?:\#(?P<ref>[^@"\s#()]+))?(?:@(?P<version>[^@"\s#()]+))?|@(?P<version2>[^@"\s#()]+)\#(?P<ref2>[^@"\s#()]+))"""
    r"""(?P=q)(?=[\s)]|$)"""
)
USES = re.compile(
    r"""^\s*-?\s*uses:\s*(?P<quote>['"]?)(?P<source>[\w.-]+/[\w.-]+)(?:/[^@\s'"]*)?@(?P<ref>[^\s'"#]+)(?P=quote)"""
)
MATRIX_IMAGE = re.compile(
    r"""(?<![\w-])(?P<kq>["']?)image(?P=kq)\s*:\s*(?P<quote>["']?)(?P<ref>[A-Za-z0-9._-]+)(?P=quote)(?=[\s,}\]]|$)"""
)
BLOCK_SCALAR = re.compile(
    r"""^(?P<lead>\s*(?:-\s+)*)(?:[^\s#'"][^:#]*|"[^"]*"|'[^']*')\s*:\s*[|>](?P<indicator>[0-9+-]*)\s*$"""
)
BRACKET = re.compile(r"(#?)\[(=*)\[")
DOCKERFILE = re.compile(
    r"(?:Dockerfile|Containerfile)(?:\.(?P<ext>[\w.-]+))?|[\w.-]+\.(?:[Dd]ockerfile|[Cc]ontainerfile)"
)
# The suffixes of a document or a backup, which a name such as `Dockerfile.md` carries.
DOCUMENTS = {"md", "markdown", "txt", "rst", "adoc", "html", "htm", "org", "bak", "orig", "old", "swp", "tmp", "rej"}
# A FROM instruction, the first one after the byte order mark a Dockerfile may open with.
FROM = re.compile(r"^\ufeff?\s*FROM\s", re.IGNORECASE | re.MULTILINE)
# The key a tag comment may write before its tag: `tag` before `=`, `:`, or a blank, and `version` before `=`, `:`,
# or a word shaped as a tag, so that `# version 4.0 is the minimum` stays prose and `# tag-v4.0.0` stays one word.
TAG_KEY = r"(?:tag(?:\s*[=:]\s*|\s+)|version(?:\s*[=:]\s*|\s+(?=(?:[A-Za-z0-9]+[-_])*v\d)))?"
COMMENT_LEAD = re.compile(r"#+\s*" + TAG_KEY, re.IGNORECASE)
# A comment that holds nothing but a tag, `# v4.0.0` or `# tag=v4.0.0`, read on the line above a reference.
TAG_LINE = re.compile(r"\s*#+\s*" + TAG_KEY + r"(?P<word>[A-Za-z0-9][\w.+-]*[\w+])\s*", re.IGNORECASE)
WORD = re.compile(r"[A-Za-z0-9](?:[\w.+-]*[\w+])?")

RECORD = ("kind", "file", "line", "source", "form", "current", "latest", "status")


class SourceError(Exception):
    """A repository or an image whose versions cannot be read."""


class LibraryError(Exception):
    """A library minato does not list, or one it does not update."""


@dataclasses.dataclass
class Label:
    """A version written beside a reference: a tag comment, or a VERSION.

    Attributes:
        kind: `comment` for the word of a `# vX.Y.Z` comment, `version` for the numbers of a
            VERSION or of the `@version` of a `gh:` reference.
        text: The text as written.
        line: Its line, from 1.
        col: Its column, from 0.
        new: The text it takes once the pin moves.
    """

    kind: str
    text: str
    line: int
    col: int
    new: str = ""


@dataclasses.dataclass
class Pin:
    """One reference of a library to another library or to an image.

    Attributes:
        kind: Where the reference sits: `cpm`, `uses`, `image`, or `matrix`.
        file: The path of its file in the clone.
        line: Its line in that file, from 1.
        source: The repository, `owner/name`, or the image it names.
        ref: The reference as written, a SHA, a tag, a branch, or a variable, empty for a `gh:`
            reference that gives a version alone.
        col: The column of the reference, from 0.
        labels: The versions written beside the reference.
        words: The words of the comment beside the reference, or of a tag line right above it,
            each with its line and column: the candidates for the tag the comment names.
        form: `tag`, `sha-tag`, `sha`, `version`, `branch`, `variable`, or `fixed`, set by resolve().
        new: The reference it moves to.
        status: `current`, `behind`, `held`, `fixed`, or `unknown`.
        error: Why the pin is unknown, when it is.
    """

    kind: str
    file: str
    line: int
    source: str
    ref: str
    col: int
    labels: list = dataclasses.field(default_factory=list)
    words: list = dataclasses.field(default_factory=list)
    form: str = ""
    new: str = ""
    status: str = ""
    error: str = ""

    def text(self, moved):
        """Write the reference and its labels as one string.

        Args:
            moved: Whether to write the values the pin moves to rather than the current ones.

        Returns:
            The reference, then each label, `<sha> # v4.0.0` or `v4.0.0 VERSION 4.0.0`.
        """
        parts = [self.new if moved else self.ref] if self.ref else []
        for label in self.labels:
            value = label.new if moved else label.text
            parts.append(f"# {value}" if label.kind == "comment" else f"VERSION {value}")
        return " ".join(parts)

    def edits(self):
        """List the replacements that move the pin.

        Returns:
            The tuples of a line, a column, the old text, and the new text, one per changed part.
        """
        out = [(self.line, self.col, self.ref, self.new)] if self.ref and self.new != self.ref else []
        out += [(lb.line, lb.col, lb.text, lb.new) for lb in self.labels if lb.new != lb.text]
        return out

    def record(self):
        """Return the fields of the pin that pins.json keeps.

        Returns:
            A dictionary of the fields named in RECORD, and the error of an unknown pin.
        """
        out = {
            "kind": self.kind,
            "file": self.file,
            "line": self.line,
            "source": self.source,
            "form": self.form,
            "current": self.text(False),
            "latest": self.text(True),
            "status": self.status,
        }
        if self.error:
            out["error"] = self.error
        return out


def version(tag, image=False):
    """Split a tag into its family and its numbers.

    Args:
        tag: A tag of a repository or of an image.
        image: Whether the tag is an image tag, which takes the form `vN` or `<name>-vN` alone.

    Returns:
        The pair of the prefix and the tuple of numbers, or None for a tag outside every family.
    """
    m = (IMAGE_TAG if image else REPO_TAG).fullmatch(tag)
    if not m:
        return None
    return m["prefix"], tuple(int(x) for x in m["number"].split("."))


def numbers(tag):
    """Return the numbers of a repository tag as written.

    Args:
        tag: A tag of a version family, `v4.1.0`.

    Returns:
        Its dotted numbers, `4.1.0`.
    """
    return REPO_TAG.fullmatch(tag)["number"]


def key(nums):
    """Pad the numbers of a version so that `4.0` and `4.0.0` compare equal.

    Args:
        nums: The tuple of numbers of a version.

    Returns:
        The tuple padded with zeros to six numbers.
    """
    return nums + (0,) * (6 - len(nums))


def preferred(names, current):
    """Choose the name of a version that carries several tags.

    Args:
        names: The tags of one version, `4.0` and `v4.0.0` on the same release.
        current: The tag the pin names now.

    Returns:
        The `vX.Y.Z` name, else the current name, else the first name in order.
    """
    full = [n for n in names if FULL_TAG.fullmatch(n)]
    if full:
        return min(full)
    return current if current in names else min(names)


def newest(tags, family, current, image=False):
    """Find the newest tag of a family.

    Args:
        tags: Tag names, of a repository or of an image.
        family: The prefix that every candidate shares, `""` for `vX.Y.Z` and `X.Y`.
        current: The tag the pin names now, kept among equal names.
        image: Whether the tags are image tags.

    Returns:
        The newest tag of the family, or None when the family has none.
    """
    best, names = None, []
    for t in tags:
        v = version(t, image)
        if not v or v[0] != family:
            continue
        if best is None or key(v[1]) > key(best):
            best, names = v[1], [t]
        elif key(v[1]) == key(best):
            names.append(t)
    return preferred(names, current) if names else None


class Remote:
    """Read the tags and heads of GitHub repositories, and the tags of container images.

    A repository is read once by `git ls-remote`, and an image once by the registry API of the
    Docker distribution specification, each answer or failure kept for the rest of the run.
    """

    def __init__(self):
        """Start with nothing read."""
        self._refs, self._images = {}, {}

    def refs(self, source):
        """Read the tags, the branches, and the head of a repository.

        Args:
            source: The repository, `owner/name`.

        Returns:
            The tags mapped to their commits, an annotated tag to the commit it tags, the branches
            mapped to their commits, and the commit of the default branch.

        Raises:
            SourceError: git cannot read the repository, or takes longer than TIMEOUT.
        """
        if source not in self._refs:
            try:
                self._refs[source] = self._read_refs(source)
            except SourceError as e:
                self._refs[source] = e
        if isinstance(self._refs[source], SourceError):
            raise self._refs[source]
        return self._refs[source]

    @staticmethod
    def _read_refs(source):
        """Run `git ls-remote` on a repository and read its answer.

        Args:
            source: The repository, `owner/name`.

        Returns:
            The tags, the branches, and the head, as refs() returns them.

        Raises:
            SourceError: git fails or takes longer than TIMEOUT.
        """
        command = ["git", "ls-remote", "--symref", f"https://github.com/{source}.git"]
        command += ["HEAD", "refs/heads/*", "refs/tags/*"]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            out = subprocess.run(command, capture_output=True, text=True, check=True, timeout=TIMEOUT, env=env).stdout
        except subprocess.CalledProcessError as e:
            raise SourceError(f"git ls-remote {source}: {e.stderr.strip() or e}") from e
        except (subprocess.TimeoutExpired, OSError) as e:
            raise SourceError(f"git ls-remote {source}: {e}") from e
        tags, heads, head = {}, {}, ""
        for line in out.splitlines():
            left, _, ref = line.partition("\t")
            if left.startswith("ref: "):
                continue
            if ref == "HEAD":
                head = left
            elif ref.startswith("refs/heads/"):
                heads[ref.removeprefix("refs/heads/")] = left
            elif ref.endswith("^{}"):
                tags[ref.removeprefix("refs/tags/").removesuffix("^{}")] = left
            elif ref.startswith("refs/tags/"):
                tags.setdefault(ref.removeprefix("refs/tags/"), left)
        return tags, heads, head

    def tags(self, source):
        """Return the tags of a repository.

        Args:
            source: The repository, `owner/name`.

        Returns:
            Its tags, mapped to their commits.
        """
        return self.refs(source)[0]

    def heads(self, source):
        """Return the branches of a repository.

        Args:
            source: The repository, `owner/name`.

        Returns:
            Its branches, mapped to their commits.
        """
        return self.refs(source)[1]

    def head(self, source):
        """Return the head of the default branch of a repository.

        Args:
            source: The repository, `owner/name`.

        Returns:
            The commit, empty when the repository has no default branch.
        """
        return self.refs(source)[2]

    def image_tags(self, image):
        """Read every tag of a container image, page after page.

        Args:
            image: The image, `host/owner/name`, `docker.io/<name>` for the Docker Hub.

        Returns:
            The list of its tags.

        Raises:
            SourceError: The registry refuses the request, fails, or answers something else than
                a tag list.
        """
        if image not in self._images:
            try:
                self._images[image] = self._read_image_tags(image)
            except SourceError as e:
                self._images[image] = e
        if isinstance(self._images[image], SourceError):
            raise self._images[image]
        return self._images[image]

    @staticmethod
    def _read_image_tags(image):
        """Read the tag list of an image from its registry, following the next pages.

        Args:
            image: The image, `host/owner/name`.

        Returns:
            Every tag, in the order of the pages.

        Raises:
            SourceError: The registry fails, refuses the request, or answers more than PAGES pages.
        """
        host, _, path = image.partition("/")
        if host == "docker.io":
            host, path = "registry-1.docker.io", path if "/" in path else f"library/{path}"
        url, token, tags = f"https://{host}/v2/{path}/tags/list?n=1000", None, []
        try:
            for _ in range(PAGES):
                headers = {"Accept": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
                try:
                    reply = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)
                except urllib.error.HTTPError as e:
                    if e.code != http.HTTPStatus.UNAUTHORIZED or token is not None:
                        raise
                    token = registry_token(e.headers.get("WWW-Authenticate", ""), path)
                    continue
                with reply:
                    tags += json.load(reply).get("tags") or []
                    link = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', reply.headers.get("Link") or "")
                if not link:
                    return tags
                url = urllib.parse.urljoin(url, link[1])
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            raise SourceError(f"registry {image}: {e}") from e
        raise SourceError(f"registry {image}: more than {PAGES} pages of tags")


def registry_token(challenge, path):
    """Obtain an anonymous pull token from the challenge of a registry.

    Args:
        challenge: The WWW-Authenticate header of a 401, `Bearer realm="...",service="..."`.
        path: The repository of the image, `owner/name`.

    Returns:
        The token.

    Raises:
        SourceError: The challenge names no realm, or the realm gives no token.
    """
    fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    if "realm" not in fields:
        raise SourceError(f"registry challenge without realm: {challenge!r}")
    query = {"scope": f"repository:{path}:pull", **({"service": fields["service"]} if "service" in fields else {})}
    with urllib.request.urlopen(f"{fields['realm']}?{urllib.parse.urlencode(query)}", timeout=TIMEOUT) as r:
        answer = json.load(r)
    token = answer.get("token") or answer.get("access_token")
    if not token:
        raise SourceError("registry token endpoint returned no token")
    return token


def files(root):
    """List the files of a clone.

    Args:
        root: The directory of the clone, the top of a git work tree or an exported tree.

    Returns:
        The paths relative to the root: the tracked files when the root is the top of a work tree,
        every file otherwise, a tree nested in another work tree included.
    """
    root = pathlib.Path(root).resolve()
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
        if pathlib.Path(top).resolve() == root:
            out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
            return sorted(p for p in out.decode().split("\0") if p)
    except (subprocess.CalledProcessError, OSError):
        pass
    found = []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".git"]
        found += [os.path.relpath(os.path.join(d, n), root) for n in names]
    return sorted(found)


def read(path):
    """Read a text file with its line endings as written.

    Args:
        path: The file.

    Returns:
        Its text, CRLF kept.
    """
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def write(path, text):
    """Write a text file with its line endings as given.

    Args:
        path: The file.
        text: Its new text, written without newline translation.
    """
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def masked(text):
    """Blank the comments of a CMake text.

    Line comments and bracket comments `#[[ ]]` become spaces, while quoted arguments, with their
    escapes, and bracket arguments `[[ ]]` stay as they are, a `#` inside them being no comment.

    Args:
        text: The text of a CMake file.

    Returns:
        The text with every comment replaced by spaces, newlines kept, so that an offset in it is
        an offset in the original.
    """
    out, i, n = list(text), 0, len(text)

    def blank(start, end):
        """Replace the characters between two offsets by spaces, newlines kept.

        Args:
            start: The first offset.
            end: The offset after the last one.
        """
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        bracket = BRACKET.match(text, i)
        if bracket and (bracket[1] or opens(text, i)):
            close = text.find("]" + bracket[2] + "]", bracket.end())
            end = n if close < 0 else close + len(bracket[2]) + 2
            if bracket[1]:
                blank(i, end)
            i = end
            continue
        if c == "#":
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
            continue
        i += 1
    return "".join(out)


def opens(text, i):
    """Tell whether a CMake argument may start at an offset.

    CMake reads `[[` as a bracket argument at the start of an argument alone, so
    `MATCHES ^[[:alnum:]]+$` holds none.

    Args:
        text: The CMake text.
        i: The offset.

    Returns:
        Whether the offset is the start of the text, or follows a blank or `(`.
    """
    return i == 0 or text[i - 1] in " \t\r\n("


def call_end(clean, start):
    """Find the closing parenthesis of a CMake call.

    Args:
        clean: The CMake text, its comments blanked by masked().
        start: The offset right after the opening parenthesis.

    Returns:
        The offset of the closing parenthesis, the parentheses of quoted and bracket arguments,
        escaped characters, and nested pairs skipped, or the end of the text.
    """
    depth, i, n = 1, start, len(clean)
    while i < n:
        c = clean[i]
        bracket = BRACKET.match(clean, i)
        if c == "\\":
            i += 1
        elif bracket and not bracket[1] and opens(clean, i):
            close = clean.find("]" + bracket[2] + "]", bracket.end())
            i = n if close < 0 else close + len(bracket[2]) + 1
        elif c == '"':
            i += 1
            while i < n and clean[i] != '"':
                i += 2 if clean[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def position(text, offset):
    """Convert an offset in a text to a line and a column.

    Args:
        text: The text.
        offset: The offset of a character.

    Returns:
        The line, from 1, and the column, from 0.
    """
    line = text.count("\n", 0, offset) + 1
    return line, offset - (text.rfind("\n", 0, offset) + 1)


def owned(source, owners):
    """Tell whether a repository belongs to an owner of minato.json, whatever the case.

    Args:
        source: The repository, `owner/name`.
        owners: The owners of minato.json, in lower case.

    Returns:
        Whether its owner is one of them.
    """
    return source.split("/")[0].lower() in owners


def comment_words(row, start, line):
    """Read the first word of a comment, the place where a tag comment writes its tag.

    A later word is prose, `# needs 4.0 or later`, and never names the tag of the reference.

    Args:
        row: A line.
        start: The column of the `#` that opens the comment.
        line: The number of the line, from 1.

    Returns:
        The word with its line and its column, a key and punctuation set apart, so that
        `# tag=v4.0.0`, `# Tag: v4.0.0`, `# version v4.0.0`, and `# (v4.0.0)` give `v4.0.0`, or an
        empty list.
    """
    lead = COMMENT_LEAD.match(row, start)
    word = WORD.search(row, lead.end()) if lead else None
    return [(word[0], line, word.start())] if word else []


def words_at(text, clean, line, col):
    """Read the comment that follows a CMake reference on its line, or the tag line above it.

    Args:
        text: The text of the file.
        clean: The text, comments blanked by masked().
        line: The line of the reference, from 1.
        col: Its column, from 0.

    Returns:
        The words of comment_words(), or those of line_above() when the line has no comment.
    """
    row_start = sum(len(r) + 1 for r in text.split("\n")[: line - 1])
    row_end = text.find("\n", row_start)
    row_end = len(text) if row_end < 0 else row_end
    hashes = [k for k in range(row_start + col, row_end) if text[k] == "#" and clean[k] == " "]
    return comment_words(text[row_start:row_end], hashes[0] - row_start, line) if hashes else line_above(text, line)


def line_above(text, line):
    """Read the tag of a comment line right above a reference.

    Args:
        text: The text of the file.
        line: The line of the reference, from 1.

    Returns:
        The word of the line above, with its line and column, when that line holds a comment and
        one word alone, `# v4.0.0` or `# tag=v4.0.0`, else an empty list.
    """
    rows = text.split("\n")
    m = TAG_LINE.fullmatch(rows[line - 2]) if line > 1 else None
    return [(m["word"], line - 1, m.start("word"))] if m else []


def scan_cmake(path, text, owners):
    """Find the pins of a CMake file.

    Args:
        path: The path of the file in the clone.
        text: Its text.
        owners: The owners whose repositories are pins, in lower case.

    Returns:
        A pin per GIT_TAG or `gh:` reference of a CPM call on a repository of an owner, with its
        tag comment and its VERSION as labels, commented calls excepted.
    """
    pins, clean = [], masked(text)
    for call in CPM_CALL.finditer(clean):
        base = call.end()
        body = clean[base : call_end(clean, base)]
        if CPM_CALL.search(body):
            continue
        uri = CPM_URI.search(body)
        if uri and not any(uri[g] for g in ("ref", "version", "ref2", "version2")):
            uri = None
        repo = CPM_GITHUB.search(body) or CPM_GIT.search(body)
        source = (uri["source"] or uri["url"]) if uri else repo["source"] if repo else None
        if not source or not owned(source, owners):
            continue
        pin = uri_pin(path, text, base, uri, source) if uri else tag_pin(path, text, clean, base, source)
        if pin:
            pin.words = pin.words or words_at(text, clean, pin.line, pin.col)
            pins.append(pin)
    return pins


def uri_pin(path, text, base, uri, source):
    """Make the pin of a `gh:owner/name#ref@version` reference.

    Args:
        path: The path of the file in the clone.
        text: Its text.
        base: The offset of the body of the CPM call.
        uri: The match of CPM_URI in that body.
        source: The repository, `owner/name`.

    Returns:
        The pin on the ref, with the version as its label, or None when the reference gives neither.
    """
    ref, rstart = (uri["ref"], uri.start("ref")) if uri["ref"] else (uri["ref2"], uri.start("ref2"))
    ver, vstart = (uri["version"], uri.start("version")) if uri["version"] else (uri["version2"], uri.start("version2"))
    if not ref and not ver:
        return None
    line, col = position(text, base + (rstart if ref else vstart))
    pin = Pin("cpm", path, line, source, ref or "", col)
    if ver:
        vline, vcol = position(text, base + vstart)
        pin.labels.append(Label("version", ver, vline, vcol))
    return pin


def tag_pin(path, text, clean, base, source):
    """Make the pin of the GIT_TAG of a CPM call.

    Args:
        path: The path of the file in the clone.
        text: Its text.
        clean: Its text, comments blanked by masked().
        base: The offset of the body of the call.
        source: The repository the call names, `owner/name`.

    Returns:
        The pin, its tag comment and its VERSION as labels, or None for a call without GIT_TAG.
    """
    body = clean[base : call_end(clean, base)]
    tag = CPM_TAG.search(body)
    ver = CPM_VERSION.search(body)
    if not tag and not ver:
        return None
    if tag:
        line, col = position(text, base + tag.start("ref"))
        pin = Pin("cpm", path, line, source, tag["ref"], col)
    else:
        # Without GIT_TAG, CPM fetches the tag `v<VERSION>`, so the VERSION is the pin.
        line, col = position(text, base + ver.start("version"))
        pin = Pin("cpm", path, line, source, "", col)
    if ver:
        vline, vcol = position(text, base + ver.start("version"))
        pin.labels.append(Label("version", ver["version"], vline, vcol))
    return pin


def comment_start(row):
    """Find where the comment of a YAML line starts.

    Args:
        row: A line of YAML.

    Returns:
        The column of the `#` that opens the comment, at the start of the line or after a blank,
        outside quoted scalars, or -1 when the line has none. A quote opens a scalar only where a
        value starts: at the start of the line, after `- `, after `: `, after `[`, `{`, and `,`, and
        inside a flow collection after a `:` that the quote follows. A flow collection opens where a
        value starts. A quote inside a plain scalar, `echo 'x`, `echo a:'b`, or `echo [WARN a:'b`,
        is a character of the scalar.
    """
    quote, value, flow, i, n = "", True, 0, 0, len(row)
    while i < n:
        c = row[i]
        after = row[i + 1] if i + 1 < n else " "
        if quote:
            if quote == '"' and c == "\\":
                i += 2
                continue
            if c == quote:
                if quote == "'" and after == "'":
                    i += 2
                    continue
                quote = ""
        elif c == "#" and (i == 0 or row[i - 1] in " \t"):
            return i
        elif c in " \t":
            pass
        elif c in "\"'" and value:
            quote, value = c, False
        else:
            # A value starts after a flow indicator, after `: `, after the `- ` of a sequence, and,
            # inside a flow collection, after a `:` that a quote follows, as JSON writes it.
            flow += (c in "[{" and value) - (c in "]}" and flow > 0)
            colon = c == ":" and (after in " \t,]}" or (flow > 0 and after in "\"'"))
            value = c in "[{," or colon or (c == "-" and value and after in " \t")
        i += 1
    return -1


def scan_yaml(path, text, owners, images, matrix):
    """Find the pins of a YAML file.

    Args:
        path: The path of the file in the clone.
        text: Its text.
        owners: The owners whose repositories are pins, in lower case.
        images: The images whose tags are pins.
        matrix: The image the short `image:` keys of the file name and the values the file gives
            them, or None outside a matrix file.

    Returns:
        A pin per `uses:` on a repository of an owner, with its tag comment as label, per full
        reference to an image, and per short `image:` of a matrix file. Comments and the content
        of block scalars, `run: |`, hold no pin.
    """
    pins, block, content, after_block = [], None, None, False
    for n, raw in enumerate(text.split("\n"), 1):
        indent = len(raw) - len(raw.lstrip())
        if block is not None:
            # The first line of the content sets its indentation, and a line indented less ends it.
            if not raw.strip() or (indent > block and (content is None or indent >= content)):
                content = indent if content is None and raw.strip() else content
                after_block = True
                continue
            block, content = None, None
        start = comment_start(raw)
        code = raw if start < 0 else raw[:start]
        opener = BLOCK_SCALAR.match(code.rstrip())
        if opener:
            # An indentation indicator, `|2`, fixes the content at the column of the key plus the digit.
            block = len(opener["lead"])
            digits = [int(d) for d in opener["indicator"] if d.isdigit()]
            content = block + digits[0] if digits else None
        m = USES.match(code)
        if m and owned(m["source"], owners):
            pin = Pin("uses", path, n, m["source"], m["ref"], m.start("ref"))
            if start >= 0:
                pin.words = comment_words(raw, start, n)
            elif not after_block:
                pin.words = line_above(text, n)
            pins.append(pin)
        pins += image_pins(path, n, code, images)
        if matrix:
            registry, values = matrix
            pins += [
                Pin("matrix", path, n, registry, i["ref"], i.start("ref"))
                for i in MATRIX_IMAGE.finditer(code)
                if i["ref"] in values
            ]
        after_block = False
    return pins


def image_pins(path, n, code, images):
    """Make a pin per full reference to an image in a line.

    Args:
        path: The path of the file in the clone.
        n: The line, from 1.
        code: The line, its comment cut away.
        images: The images whose tags are pins.

    Returns:
        The pins of the line.
    """
    found = (
        (image, i)
        for image in images
        for i in re.finditer(
            r"(?<![\w./-])" + re.escape(image) + r":(?P<ref>[A-Za-z0-9._-]+)(?P<digest>@sha256:[0-9a-f]{64})?", code
        )
    )
    return [
        Pin("image", path, n, image, i["ref"], i.start("ref"), form="digest" if i["digest"] else "")
        for image, i in found
    ]


def dockerfile(name):
    """Tell whether a file name is the name of a Dockerfile.

    Args:
        name: The base name of a file.

    Returns:
        Whether it is `Dockerfile`, `Containerfile`, one of them with a suffix, `Dockerfile.dev`, or
        a name ending in `.Dockerfile`, the suffixes of a document or a backup, `Dockerfile.md` and
        `Dockerfile.bak`, excepted.
    """
    m = DOCKERFILE.fullmatch(name)
    return bool(m) and (m["ext"] or "").rsplit(".", 1)[-1].lower() not in DOCUMENTS


def scan_dockerfile(path, text, images):
    """Find the full image references of a Dockerfile.

    Args:
        path: The path of the file in the clone.
        text: Its text.
        images: The images whose tags are pins.

    Returns:
        A pin per reference, comment lines excepted, and none for a file without a FROM instruction.
    """
    if not FROM.search(text):
        return []
    pins = []
    for n, raw in enumerate(text.split("\n"), 1):
        if not raw.lstrip().startswith("#"):
            pins += image_pins(path, n, raw, images)
    return pins


def matches(path, patterns):
    """Match a path of a clone against globs.

    Args:
        path: The path, relative to the root of the clone.
        patterns: A glob or a list of globs, where a leading `**/` matches the root as well.

    Returns:
        Whether the path matches one of them.
    """
    patterns = [patterns] if isinstance(patterns, str) else patterns
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch("/" + path, p) for p in patterns)


def matrix_image(path, text, matrices):
    """Read the image a matrix file names, and the values its `image` keys take.

    Args:
        path: The path of the file in the clone.
        text: Its text, YAML or JSON.
        matrices: The `matrices` entry of minato.json, its globs and its registry.

    Returns:
        The registry of minato.json and the set of the values of the top `image` and of the
        `image` of each row, or None for a file outside the globs, a file that does not parse, and
        a matrix that sets another `registry`.

    Raises:
        LibraryError: PyYAML is not installed.
    """
    if not matrices or not matches(path, matrices["files"]):
        return None
    try:
        # Imported here, so that a clone without a matrix file is measured without PyYAML.
        import yaml  # noqa: PLC0415
    except ImportError as e:
        raise LibraryError(f"{path}: a matrix file needs PyYAML, pip install -r requirements.txt") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or data.get("registry", matrices["registry"]) != matrices["registry"]:
        return None
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    values = {str(x["image"]) for x in [data, *rows] if isinstance(x, dict) and x.get("image") is not None}
    return matrices["registry"], values


def scan(root, config):
    """Find every pin of a clone.

    Args:
        root: The directory of the clone.
        config: The content of minato.json.

    Returns:
        The pins of its CMake files, YAML files, matrix files, and Dockerfiles, unresolved. A
        symbolic link, and a file that resolves outside the clone, are skipped.
    """
    root = pathlib.Path(root).resolve()
    owners = {o.lower() for o in config["owners"]}
    images = list(config["images"])
    matrices = config.get("matrices") or {}
    pins = []
    for path in files(root):
        full = root / path
        name = os.path.basename(path)
        is_cmake = name == "CMakeLists.txt" or name.endswith(".cmake")
        is_yaml = name.endswith((".yml", ".yaml")) or (
            name.endswith(".json") and matches(path, matrices.get("files", []))
        )
        is_docker = dockerfile(name)
        if not (is_cmake or is_yaml or is_docker) or full.is_symlink() or not full.is_file():
            continue
        if not full.resolve().is_relative_to(root):
            continue
        try:
            text = read(full)
        except UnicodeDecodeError:
            continue
        if is_cmake:
            pins += scan_cmake(path, text, owners)
        elif is_docker:
            pins += scan_dockerfile(path, text, images)
        else:
            pins += scan_yaml(path, text, owners, images, matrix_image(path, text, matrices))
    return pins


def settle(pin, form, status, error=""):
    """Mark a pin that does not move.

    Args:
        pin: The pin.
        form: Its form.
        status: Its status, `fixed` or `unknown`.
        error: Why the pin is unknown.

    Returns:
        The pin, its new values its current ones.
    """
    pin.form, pin.new, pin.status, pin.error = form, pin.ref, status, error
    for label in pin.labels:
        label.new = label.text
    return pin


def label_tag(label, tags):
    """Name the tag a label stands for.

    A comment names its tag. A VERSION names `v<version>`, the GIT_TAG CPM derives from it, or the
    tag spelled as the version.

    Args:
        label: The label.
        tags: The tags of the source.

    Returns:
        The tag, or None when the source has no such tag.
    """
    names = [label.text] if label.kind == "comment" else [f"v{label.text}", label.text]
    return next((t for t in names if t in tags), None)


def relabel(pin, name):
    """Write the new tag into every label of a pin.

    A comment takes the name of the tag, a VERSION its numbers. A VERSION beside a tag outside
    every family keeps its text.

    Args:
        pin: The pin.
        name: The tag it moves to.
    """
    for label in pin.labels:
        if label.kind == "comment":
            label.new = name
        else:
            label.new = numbers(name) if version(name) else label.text


def tag_labels(pin, tags):
    """Make the comment label of a pin from the first word of its comment that is a tag of its source.

    On a SHA, that word anchors the SHA, which resolve_sha() then checks. On a tag, it stays a label
    only when it names the commit of the tag, so prose that names another release is left alone.

    Args:
        pin: The pin.
        tags: The tags of its source.

    Returns:
        When no word is a tag, the words that have the shape of one, `v5.0.0`, else an empty list.
    """
    pin.labels = [lb for lb in pin.labels if lb.kind != "comment"]
    tagged = [w for w in pin.words if w[0] in tags]
    alone = next((lb for lb in pin.labels if lb.kind == "version"), None)
    anchor = pin.ref or (label_tag(alone, tags) if alone else None)
    if tagged and (SHA.fullmatch(pin.ref) or tags[tagged[0][0]] == tags.get(anchor)):
        pin.labels.insert(0, Label("comment", *tagged[0]))
    return [] if tagged else [w[0] for w in pin.words if TAG_SHAPED.fullmatch(w[0])]


def resolve_image(pin, remote):
    """Resolve an image or matrix pin against the tags of its image.

    Args:
        pin: The pin.
        remote: The source of the tags, a Remote.

    Returns:
        The pin: fixed outside every family, unknown when the registry does not list its tag or a
        digest follows it.

    Raises:
        SourceError: The registry cannot be read.
    """
    if pin.form == "digest":
        return settle(pin, "digest", "unknown", f"a digest pins {pin.source}:{pin.ref}, which a new tag would not move")
    v = version(pin.ref, image=True)
    if not v:
        return settle(pin, "fixed", "fixed")
    tags = remote.image_tags(pin.source)
    if pin.ref not in tags:
        return settle(pin, "tag", "unknown", f"{pin.ref} is not a tag of {pin.source}")
    pin.form, pin.new = "tag", newest(tags, v[0], pin.ref, image=True)
    return pin


def resolve_sha(pin, remote):
    """Resolve a pin on a SHA.

    A label that names a tag of the source anchors the SHA to that tag, and the pin moves to the
    commit of the newest tag of its family. Without label, it moves to the head of the default
    branch.

    Args:
        pin: The pin.
        remote: The source of the tags and heads, a Remote.

    Returns:
        The pin: unknown when a label names no tag, or when the SHA is not the commit of its tag.

    Raises:
        SourceError: The repository cannot be read.
    """
    tags = remote.tags(pin.source)
    missing = tag_labels(pin, tags)
    if missing:
        return settle(pin, "sha-tag", "unknown", f"the comment names {missing[0]}, which is not a tag of {pin.source}")
    anchors = [label_tag(lb, tags) for lb in pin.labels]
    if any(a is None for a in anchors):
        return settle(pin, "sha-tag", "unknown", "a VERSION beside the SHA names no tag of the source")
    if not anchors:
        pin.form, pin.new = "sha", remote.head(pin.source)
        if not pin.new:
            return settle(pin, "sha", "unknown", f"{pin.source} has no default branch")
        return pin
    tag = anchors[0]
    if any(tags[a] != tags[tag] for a in anchors) or tags[tag] != pin.ref:
        return settle(pin, "sha-tag", "unknown", f"{pin.ref} is not the commit of {tag}")
    name = newest(tags, version(tag)[0], tag) if version(tag) else tag
    pin.form, pin.new = "sha-tag", tags[name]
    relabel(pin, name)
    return pin


def resolve_tag(pin, remote):
    """Resolve a pin on a tag, or on a version alone, of a `gh:` reference or of a call without GIT_TAG.

    Args:
        pin: The pin.
        remote: The source of the tags, a Remote.

    Returns:
        The pin: fixed outside every family, unknown when the source has no such tag, or when the
        newest tag cannot be written as a CPM version.

    Raises:
        SourceError: The repository cannot be read.
    """
    tags = remote.tags(pin.source)
    tag_labels(pin, tags)
    alone = next((lb for lb in pin.labels if lb.kind == "version"), None) if not pin.ref else None
    current = pin.ref or label_tag(alone, tags)
    if not current:
        return settle(pin, "version", "unknown", f"v{alone.text} is not a tag of {pin.source}")
    v = version(current)
    if not v:
        return settle(pin, "fixed", "fixed")
    if current not in tags:
        return settle(pin, "tag", "unknown", f"{current} is not a tag of {pin.source}")
    name = newest(tags, v[0], current)
    if not pin.ref and not re.fullmatch(r"v\d+(?:\.\d+)*", name):
        return settle(pin, "version", "unknown", f"{name} cannot be written as a CPM version")
    pin.form, pin.new = ("tag", name) if pin.ref else ("version", "")
    relabel(pin, name)
    return pin


def resolve(pin, remote):
    """Set the form, the new values, and the status of a pin.

    Args:
        pin: A pin from scan().
        remote: The source of the tags and heads, a Remote.

    Returns:
        The same pin, resolved: `unknown` with its error when its source cannot be read.
    """
    try:
        if pin.kind in ("image", "matrix"):
            resolve_image(pin, remote)
        elif "${" in pin.ref or any("${" in lb.text for lb in pin.labels):
            settle(pin, "variable", "fixed")
        elif SHA.fullmatch(pin.ref):
            resolve_sha(pin, remote)
        elif pin.ref and pin.ref in remote.heads(pin.source):
            settle(pin, "branch", "fixed")
        else:
            resolve_tag(pin, remote)
    except SourceError as e:
        settle(pin, pin.form or "tag", "unknown", str(e))
    if not pin.status:
        pin.status = "behind" if pin.edits() else "current"
    return pin


def held(pin, holds):
    """Find the hold that keeps a pin.

    Args:
        pin: The pin.
        holds: The content of the holds.json of its library.

    Returns:
        The first hold on the source of the pin whose globs match its file, or None.
    """
    return next(
        (h for h in holds if h["source"].lower() == pin.source.lower() and matches(pin.file, h.get("files", ["*"]))),
        None,
    )


def measure(root, config, remote, holds=()):
    """Find and resolve every pin of a clone.

    Args:
        root: The directory of the clone.
        config: The content of minato.json.
        remote: The source of the tags and heads, a Remote.
        holds: The content of the holds.json of the library.

    Returns:
        The pins, a behind pin under a hold marked `held`.
    """
    pins = [resolve(p, remote) for p in scan(root, config)]
    for p in pins:
        if p.status == "behind" and held(p, holds):
            p.status = "held"
    return pins


def moved(text, pins):
    """Rewrite the behind pins of one file.

    Args:
        text: The text of the file.
        pins: The pins of that file.

    Returns:
        The text, each part of each behind pin replaced and the rest of each line kept.

    Raises:
        ValueError: A part no longer sits where scan() found it, or two parts overlap.
    """
    lines = text.split("\n")
    edits = sorted((e for p in pins if p.status == "behind" for e in p.edits()), key=lambda e: (e[0], -e[1]))
    for k, (line, col, old, new) in enumerate(edits):
        row = lines[line - 1]
        if row[col : col + len(old)] != old:
            raise ValueError(f"line {line}: {old} is not at column {col}")
        if k and edits[k - 1][0] == line and col + len(old) > edits[k - 1][1]:
            raise ValueError(f"line {line}: two replacements overlap at column {col}")
        lines[line - 1] = row[:col] + new + row[col + len(old) :]
    return "\n".join(lines)


def changes(root, pins):
    """Compute the files a move changes.

    Args:
        root: The directory of the clone.
        pins: Its resolved pins.

    Returns:
        Each file with a behind pin, mapped to its text before and after the move.
    """
    by_file = {}
    for p in pins:
        if p.status == "behind":
            by_file.setdefault(p.file, []).append(p)
    out = {}
    for path, group in sorted(by_file.items()):
        before = read(pathlib.Path(root) / path)
        out[path] = (before, moved(before, group))
    return out


def short(text):
    """Write every full SHA of a pin text with its first seven characters, as GitHub shows them.

    Args:
        text: The text of a pin, its reference and its labels.

    Returns:
        The same text with each full SHA shortened.
    """
    return re.sub(rf"\b{SHA.pattern}\b", lambda m: m.group(0)[:7], text)


def joined(names):
    """Join names as an English list, with the serial comma from three names on.

    Args:
        names: The names, at least one.

    Returns:
        `a`, `a and b`, or `a, b, and c`.
    """
    *first, last = names
    if not first:
        return last
    return f"{first[0]} and {last}" if len(first) == 1 else ", ".join(first) + f", and {last}"


def pull_text(pins):
    """Write the title and the body of the pull request that moves the pins behind.

    Args:
        pins: The resolved pins of a library.

    Returns:
        The title and the body, or None when no pin is behind. The title names the dependency and
        its new version when one move covers every pin, and the dependencies otherwise. The body
        gives one entry per dependency and move, with the lines the move changes.
    """
    moves = {}
    for p in pins:
        if p.status == "behind":
            sites = moves.setdefault(p.source, {}).setdefault((short(p.text(False)), short(p.text(True))), [])
            sites.append(f"{p.file}:{p.line}")
    if not moves:
        return None
    names = [source.rsplit("/", 1)[-1] for source in moves]
    if len(moves) == 1 and len(next(iter(moves.values()))) == 1:
        title = f"Move {names[0]} to {next(iter(next(iter(moves.values()))))[1]}"
    else:
        title = f"Move the pins of {joined(names)}"
    body = []
    for source, by_move in moves.items():
        for (old, new), sites in by_move.items():
            body.append(f"- `{source}`: `{old}` to `{new}`")
            body += [f"  - `{site}`" for site in sites]
    return title, "\n".join(body) + "\n"


def load(path, default):
    """Read a JSON file.

    Args:
        path: The file.
        default: The value of a missing file.

    Returns:
        Its content, or the default.
    """
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def library(name):
    """Read the library.json of a library.

    Args:
        name: The folder of the library under libraries/.

    Returns:
        Its content.

    Raises:
        LibraryError: minato lists no library of that name.
    """
    found = load(ROOT / "libraries" / name / "library.json", None)
    if found is None:
        raise LibraryError(f"no library {name!r} under {ROOT / 'libraries'}")
    return found


def library_pins(name, clone, config, remote):
    """Measure the pins of a library under its holds.

    Args:
        name: The folder of the library under libraries/.
        clone: The directory of its clone.
        config: The content of minato.json.
        remote: The source of the tags and heads, a Remote.

    Returns:
        Its resolved pins.

    Raises:
        LibraryError: minato lists no library of that name, its holds.json is malformed, or a
            matrix file of the clone needs PyYAML, which is not installed.
    """
    library(name)
    path = ROOT / "libraries" / name / "holds.json"
    holds = load(path, [])
    if not isinstance(holds, list) or not all(
        isinstance(h, dict)
        and isinstance(h.get("source"), str)
        and isinstance(h.get("files", []), list)
        and all(isinstance(g, str) for g in h.get("files", []))
        for h in holds
    ):
        raise LibraryError(f"{path}: a list of holds, each with a source and an optional list of globs")
    return measure(clone, config, remote, holds)


def run_scan(args, config, remote):
    """Print the pins of one clone as JSON.

    Args:
        args: The arguments of the command, `clone` and `library`.
        config: The content of minato.json.
        remote: The source of the tags and heads, a Remote.

    Returns:
        The exit status, 0.
    """
    pins = (
        library_pins(args.library, args.clone, config, remote) if args.library else measure(args.clone, config, remote)
    )
    json.dump([p.record() for p in pins], sys.stdout, indent=1)
    print()
    return 0


def run_apply(args, config, remote):
    """Move the pins of one clone in place, and print one line per pin moved.

    Args:
        args: The arguments of the command, `library`, `clone`, and `summary`, the file of the title and the body.
        config: The content of minato.json.
        remote: The source of the tags and heads, a Remote.

    Returns:
        The exit status, 0.

    Raises:
        LibraryError: minato lists no such library, or does not update it.
    """
    if not library(args.library).get("updates", True):
        raise LibraryError(f"minato does not update {args.library}: its library.json sets updates to false")
    pins = library_pins(args.library, args.clone, config, remote)
    for path, (_, after) in changes(args.clone, pins).items():
        write(pathlib.Path(args.clone) / path, after)
    text = pull_text(pins)
    if args.summary and text:
        record = json.dumps({"title": text[0], "body": text[1]}, indent=1)
        pathlib.Path(args.summary).write_text(record + "\n", encoding="utf-8")
    for p in pins:
        if p.status == "behind":
            print(f"{p.file}:{p.line}: {p.source} {p.text(False)} -> {p.text(True)}")
        elif p.status == "unknown":
            print(f"{p.file}:{p.line}: {p.source} unknown: {p.error}", file=sys.stderr)
    return 0


def run_check(args, config, remote):
    """Measure every library, write its pins.json and show its changes on request.

    Args:
        args: The arguments of the command, `clones`, `write`, and `diff`.
        config: The content of minato.json.
        remote: The source of the tags and heads, a Remote.

    Returns:
        The exit status, 0, or 1 when a library could not be measured.
    """
    status = 0
    for folder in sorted((ROOT / "libraries").iterdir()):
        clone = pathlib.Path(args.clones) / folder.name
        try:
            entry = library(folder.name)
            if not clone.is_dir():
                raise LibraryError(f"no clone in {args.clones}")
            pins = library_pins(folder.name, clone, config, remote)
            diffs = changes(clone, pins) if args.diff else {}
        except (LibraryError, OSError, ValueError) as e:
            print(f"== {folder.name}: {e}", file=sys.stderr)
            status = 1
            continue
        counts = {}
        for p in pins:
            counts[p.status] = counts.get(p.status, 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        print(f"== {folder.name}: {summary}" + ("" if entry.get("updates", True) else ", no pull request"))
        for p in pins:
            if p.error:
                print(f"   {p.file}:{p.line}: {p.error}")
        if args.write:
            record = load(folder / "pins.json", {"pull": None})
            record["pins"] = [p.record() for p in pins]
            (folder / "pins.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        for path, (before, after) in diffs.items():
            diff = difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}")
            sys.stdout.writelines(diff)
    return status


def main(argv=None):
    """Run one command of the script.

    Args:
        argv: The arguments, those of the command line by default.

    Returns:
        The exit status of the command, 2 for a library minato does not list or does not update.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("clone")
    scan_parser.add_argument("--library", default="")
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--clones", required=True)
    check_parser.add_argument("--write", action="store_true")
    check_parser.add_argument("--diff", action="store_true")
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("library")
    apply_parser.add_argument("clone")
    apply_parser.add_argument("--summary", default="")
    args = parser.parse_args(argv)
    commands = {"scan": run_scan, "apply": run_apply, "check": run_check}
    try:
        return commands[args.command](args, load(ROOT / "minato.json", None), Remote())
    except LibraryError as e:
        print(f"pins.py: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
