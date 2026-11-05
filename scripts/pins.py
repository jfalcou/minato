#!/usr/bin/env python3
"""Measure the pins of the libraries on each other, and move them to the newest versions.

Usage:
    pins.py scan <clone> [--library <name>]
        The pins of one clone, as JSON, under the holds of the library when one is named.
    pins.py check --clones <dir> [--write] [--diff]
        Every library under libraries/, its clone in <dir>/<name>: a count per status, its
        pins.json written with --write, and the changes a move would make with --diff.
    pins.py apply <library> <clone>
        The pins of one clone moved in place, one line printed per pin moved.

A pin names a repository of an owner of minato.json, or a tag of one of its images:
    - the GIT_TAG of a CPMAddPackage, CPMDeclarePackage, or CPMFindPackage call,
    - the ref of a `uses:` in a YAML file,
    - a full image reference, such as `ghcr.io/jfalcou/compilers:v11`,
    - the `image:` of a matrix file, a tag of the registry minato.json names.

A tag moves to the newest tag of its family, a SHA followed by a tag comment to the commit of the
newest tag of the comment's family, and a bare SHA to the head of the default branch. A branch, a
CMake variable, and a tag outside every family stay.
"""

import argparse
import dataclasses
import difflib
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent

SHA = re.compile(r"[0-9a-f]{40}")
# A repository tag: a prefix, an optional v, and dotted numbers, `4.0`, `v4.0.0`, `hpx-v1.9.0`.
REPO_TAG = re.compile(r"(?P<prefix>(?:[A-Za-z0-9]+[-_])*)(?P<v>v?)(?P<number>\d+(?:\.\d+)*)")
# An image tag: `vN`, or a name and `-vN`, `sycl-v1`. Any other tag of an image is ignored.
IMAGE_TAG = re.compile(r"(?P<prefix>(?:[a-z0-9]+-)?)(?P<v>v)(?P<number>\d+)")
FULL_TAG = re.compile(r"(?:[A-Za-z0-9]+[-_])*v\d+\.\d+\.\d+")

CPM_CALL = re.compile(r"\b(?:CPMAddPackage|CPMDeclarePackage|CPMFindPackage)\s*\(", re.IGNORECASE)
CPM_GITHUB = re.compile(r"\bGITHUB_REPOSITORY\s+\"?(?P<source>[\w.-]+/[\w.-]+?)\"?(?=[\s)])")
CPM_GIT = re.compile(r"\bGIT_REPOSITORY\s+\"?https://github\.com/(?P<source>[\w.-]+/[\w.-]+?)(?:\.git)?\"?(?=[\s)])")
CPM_TAG = re.compile(r"\bGIT_TAG\s+(?P<quote>\"?)(?P<ref>[^\s\")]+)(?P=quote)")
USES = re.compile(r"""^\s*-?\s*uses:\s*['"]?(?P<source>[\w.-]+/[\w.-]+)(?:/[^@\s'"]*)?@(?P<ref>[^\s'"#]+)""")
COMMENT = re.compile(r"\s*#\s*(?P<tag>\S+)")
MATRIX_IMAGE = re.compile(r"\bimage:\s*(?P<quote>\"?)(?P<ref>[A-Za-z0-9._-]+)(?P=quote)(?=[\s,}]|$)")
MATRIX_REGISTRY = re.compile(r"^registry:\s*\"?(?P<registry>\S+?)\"?\s*$", re.MULTILINE)

RECORD = ("kind", "file", "line", "source", "form", "current", "latest", "status")


@dataclasses.dataclass
class Pin:
    """One reference of a library to another library or to an image.

    Attributes:
        kind: Where the reference sits: `cpm`, `uses`, `image`, or `matrix`.
        file: The path of its file in the clone.
        line: Its line in that file, from 1.
        source: The repository, `owner/name`, or the image it names.
        form: `tag`, `sha-tag`, `sha`, `branch`, `variable`, or `fixed`, set by resolve().
        current: The reference as written, a `sha-tag` pin carrying its comment, `<sha> # v4.0.0`.
        latest: The reference it moves to, in the same form.
        status: `current`, `behind`, `held`, `fixed`, or `unknown`.
        col: The column of the reference in its line, from 0.
        tag: The tag of the comment that follows a SHA.
        tag_col: The column of that tag.
    """

    kind: str
    file: str
    line: int
    source: str
    form: str
    current: str
    latest: str = ""
    status: str = ""
    col: int = 0
    tag: str = ""
    tag_col: int = 0

    def record(self):
        """Return the fields of the pin that pins.json keeps.

        Returns:
            A dictionary of the fields named in RECORD.
        """
        return {k: getattr(self, k) for k in RECORD}


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


def key(numbers):
    """Pad the numbers of a version so that `4.0` and `4.0.0` compare equal.

    Args:
        numbers: The tuple of numbers of a version.

    Returns:
        The tuple padded with zeros to six numbers.
    """
    return numbers + (0,) * (6 - len(numbers))


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

    A repository is read once by `git ls-remote`, and an image once by the registry API with an
    anonymous token, each kept for the rest of the run.
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
            subprocess.CalledProcessError: The repository cannot be read.
        """
        if source not in self._refs:
            out = subprocess.run(
                [
                    "git",
                    "ls-remote",
                    "--symref",
                    f"https://github.com/{source}.git",
                    "HEAD",
                    "refs/heads/*",
                    "refs/tags/*",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
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
            self._refs[source] = (tags, heads, head)
        return self._refs[source]

    def tags(self, source):
        """Return the tags of a repository, mapped to their commits."""
        return self.refs(source)[0]

    def heads(self, source):
        """Return the branches of a repository, mapped to their commits."""
        return self.refs(source)[1]

    def head(self, source):
        """Return the commit of the default branch of a repository."""
        return self.refs(source)[2]

    def image_tags(self, image):
        """Read the tags of a container image.

        Args:
            image: The image, `host/owner/name`.

        Returns:
            The list of its tags.
        """
        if image not in self._images:
            host, _, path = image.partition("/")
            with urllib.request.urlopen(f"https://{host}/token?scope=repository:{path}:pull") as r:
                token = json.load(r)["token"]
            request = urllib.request.Request(
                f"https://{host}/v2/{path}/tags/list?n=1000", headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(request) as r:
                self._images[image] = list(json.load(r).get("tags") or [])
        return self._images[image]


def files(root):
    """List the files of a clone.

    Args:
        root: The directory of the clone, a git work tree or an exported tree.

    Returns:
        The paths relative to the root, the tracked files of a work tree, every file otherwise.
    """
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        return sorted(p for p in out.decode().split("\0") if p)
    except (subprocess.CalledProcessError, FileNotFoundError):
        found = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x != ".git"]
            found += [os.path.relpath(os.path.join(d, n), root) for n in names]
        return sorted(found)


def masked(text):
    """Blank the comments of a CMake text.

    Args:
        text: The text of a CMake file.

    Returns:
        The text with every comment replaced by spaces, so that an offset in it is an offset in the
        original.
    """
    out, quoted, comment = [], False, False
    for c in text:
        if comment:
            comment = c != "\n"
            out.append(c if c == "\n" else " ")
        elif c == '"':
            quoted = not quoted
            out.append(c)
        elif c == "#" and not quoted:
            comment = True
            out.append(" ")
        else:
            out.append(c)
    return "".join(out)


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


def comment_of(pin, row):
    """Record on a pin the tag of the comment that follows its reference.

    Args:
        pin: The pin, its column set.
        row: The line of the pin.
    """
    c = COMMENT.match(row, pin.col + len(pin.current))
    if c:
        pin.tag, pin.tag_col = c["tag"], c.start("tag")


def scan_cmake(path, text, owners):
    """Find the pins of a CMake file.

    Args:
        path: The path of the file in the clone.
        text: Its text.
        owners: The owners whose repositories are pins.

    Returns:
        A pin per GIT_TAG of a CPM call on a repository of an owner, commented calls excepted.
    """
    pins, clean = [], masked(text)
    rows = text.split("\n")
    for call in CPM_CALL.finditer(clean):
        end = clean.find(")", call.end())
        body = clean[call.end() : end if end >= 0 else len(clean)]
        repo = CPM_GITHUB.search(body) or CPM_GIT.search(body)
        tag = CPM_TAG.search(body)
        if not repo or not tag or repo["source"].split("/")[0] not in owners:
            continue
        line, col = position(text, call.end() + tag.start("ref"))
        pin = Pin("cpm", path, line, repo["source"], "", tag["ref"], col=col)
        comment_of(pin, rows[line - 1])
        pins.append(pin)
    return pins


def scan_yaml(path, text, owners, images, matrix):
    """Find the pins of a YAML file.

    Args:
        path: The path of the file in the clone.
        text: Its text.
        owners: The owners whose repositories are pins.
        images: The images whose tags are pins.
        matrix: The image the short `image:` keys of the file name, empty outside a matrix file.

    Returns:
        A pin per `uses:` on a repository of an owner, per full reference to an image, and per
        short `image:` of a matrix file, commented lines excepted.
    """
    pins = []
    rows = text.split("\n")
    for n, raw in enumerate(rows, 1):
        if raw.lstrip().startswith("#"):
            continue
        m = USES.match(raw)
        if m and m["source"].split("/")[0] in owners:
            pin = Pin("uses", path, n, m["source"], "", m["ref"], col=m.start("ref"))
            comment_of(pin, raw)
            pins.append(pin)
        pins += [
            Pin("image", path, n, image, "", i["ref"], col=i.start("ref"))
            for image in images
            for i in re.finditer(re.escape(image) + r":(?P<ref>[A-Za-z0-9._-]+)", raw)
        ]
        if matrix:
            pins += [
                Pin("matrix", path, n, matrix, "", i["ref"], col=i.start("ref")) for i in MATRIX_IMAGE.finditer(raw)
            ]
    return pins


def matches(path, pattern):
    """Match a path of a clone against a glob.

    Args:
        path: The path, relative to the root of the clone.
        pattern: The glob, where a leading `**/` matches the root as well.

    Returns:
        Whether the path matches.
    """
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch("/" + path, pattern)


def matrix_image(path, text, matrices):
    """Name the image the short `image:` keys of a file refer to.

    Args:
        path: The path of the file in the clone.
        text: Its text.
        matrices: The `matrices` entry of minato.json, its glob and its registry.

    Returns:
        The registry of minato.json for a matrix file that sets no other, else an empty string.
    """
    if not matrices or not matches(path, matrices["files"]):
        return ""
    own = MATRIX_REGISTRY.search(text)
    return matrices["registry"] if not own or own["registry"] == matrices["registry"] else ""


def scan(root, config):
    """Find every pin of a clone.

    Args:
        root: The directory of the clone.
        config: The content of minato.json.

    Returns:
        The pins of its CMake and YAML files, unresolved.
    """
    owners, images = set(config["owners"]), list(config["images"])
    matrices = config.get("matrices") or {}
    pins = []
    for path in files(root):
        full = pathlib.Path(root) / path
        name = os.path.basename(path)
        is_cmake = name == "CMakeLists.txt" or name.endswith(".cmake")
        is_yaml = name.endswith((".yml", ".yaml"))
        if not (is_cmake or is_yaml) or not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if is_cmake:
            pins += scan_cmake(path, text, owners)
        else:
            pins += scan_yaml(path, text, owners, images, matrix_image(path, text, matrices))
    return pins


def settle(pin, form, status):
    """Mark a pin that does not move.

    Args:
        pin: The pin.
        form: Its form.
        status: Its status, `fixed` or `unknown`.

    Returns:
        The pin, its latest reference its current one.
    """
    pin.form, pin.latest, pin.status = form, pin.current, status
    return pin


def resolve(pin, remote):
    """Set the form, the newest reference, and the status of a pin.

    Args:
        pin: A pin from scan().
        remote: The source of the tags and heads, a Remote.

    Returns:
        The same pin, resolved.
    """
    if pin.kind in ("image", "matrix"):
        v = version(pin.current, image=True)
        if not v:
            return settle(pin, "fixed", "fixed")
        pin.form = "tag"
        pin.latest = newest(remote.image_tags(pin.source), v[0], pin.current, image=True) or pin.current
    elif "${" in pin.current:
        return settle(pin, "variable", "fixed")
    elif SHA.fullmatch(pin.current):
        tags = remote.tags(pin.source)
        v = version(pin.tag) if pin.tag else None
        if v:
            pin.form = "sha-tag"
            name = newest(tags, v[0], pin.tag)
            pin.latest = f"{tags[name]} # {name}" if name else ""
            pin.current = f"{pin.current} # {pin.tag}"
        else:
            pin.form, pin.latest = "sha", remote.head(pin.source)
    elif pin.current in remote.heads(pin.source):
        return settle(pin, "branch", "fixed")
    else:
        tags = remote.tags(pin.source)
        v = version(pin.current)
        if not v:
            return settle(pin, "fixed", "fixed")
        if pin.current not in tags:
            return settle(pin, "tag", "unknown")
        pin.form, pin.latest = "tag", newest(tags, v[0], pin.current)
    pin.status = "unknown" if not pin.latest else "current" if pin.latest == pin.current else "behind"
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
        (h for h in holds if h["source"] == pin.source and any(matches(pin.file, g) for g in h.get("files", ["*"]))),
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
        The text, each behind pin replaced by its latest reference and the rest of each line kept.

    Raises:
        ValueError: A pin no longer sits where scan() found it.
    """
    lines = text.split("\n")
    for p in sorted(pins, key=lambda p: (p.line, -p.col)):
        if p.status != "behind":
            continue
        row = lines[p.line - 1]
        old, _, _ = p.current.partition(" # ")
        new, _, name = p.latest.partition(" # ")
        if row[p.col : p.col + len(old)] != old:
            raise ValueError(f"{p.file}:{p.line}: {old} is not at column {p.col}")
        if p.form == "sha-tag":
            row = row[: p.tag_col] + name + row[p.tag_col + len(p.tag) :]
        lines[p.line - 1] = row[: p.col] + new + row[p.col + len(old) :]
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
        before = (pathlib.Path(root) / path).read_text(encoding="utf-8")
        out[path] = (before, moved(before, group))
    return out


def load(path, default):
    """Read a JSON file.

    Args:
        path: The file.
        default: The value of a missing file.

    Returns:
        Its content, or the default.
    """
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def library_pins(name, clone, config, remote):
    """Measure the pins of a library under its holds.

    Args:
        name: The folder of the library under libraries/.
        clone: The directory of its clone.
        config: The content of minato.json.
        remote: The source of the tags and heads, a Remote.

    Returns:
        Its resolved pins.
    """
    return measure(clone, config, remote, load(ROOT / "libraries" / name / "holds.json", []))


def run_scan(args, config, remote):
    """Print the pins of one clone as JSON."""
    pins = (
        library_pins(args.library, args.clone, config, remote) if args.library else measure(args.clone, config, remote)
    )
    json.dump([p.record() for p in pins], sys.stdout, indent=1)
    print()


def run_apply(args, config, remote):
    """Move the pins of one clone in place, and print one line per pin moved."""
    pins = library_pins(args.library, args.clone, config, remote)
    for path, (_, after) in changes(args.clone, pins).items():
        (pathlib.Path(args.clone) / path).write_text(after, encoding="utf-8")
    for p in pins:
        if p.status == "behind":
            print(f"{p.file}:{p.line}: {p.source} {p.current} -> {p.latest}")


def run_check(args, config, remote):
    """Measure every library, write its pins.json and show its changes on request."""
    for folder in sorted((ROOT / "libraries").iterdir()):
        library = load(folder / "library.json", None)
        clone = pathlib.Path(args.clones) / folder.name
        if not library or not clone.is_dir():
            print(f"== {folder.name}: no clone in {args.clones}", file=sys.stderr)
            continue
        pins = library_pins(folder.name, clone, config, remote)
        counts = {}
        for p in pins:
            counts[p.status] = counts.get(p.status, 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        print(f"== {folder.name}: {summary}" + ("" if library.get("updates", True) else ", no pull request"))
        if args.write:
            record = load(folder / "pins.json", {"pull": None})
            record["pins"] = [p.record() for p in pins]
            (folder / "pins.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        if args.diff:
            for path, (before, after) in changes(clone, pins).items():
                diff = difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}")
                sys.stdout.writelines(diff)


def main(argv=None):
    """Run one command of the script.

    Args:
        argv: The arguments, those of the command line by default.

    Returns:
        The exit status, 0.
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
    args = parser.parse_args(argv)
    commands = {"scan": run_scan, "apply": run_apply, "check": run_check}
    commands[args.command](args, load(ROOT / "minato.json", None), Remote())
    return 0


if __name__ == "__main__":
    sys.exit(main())
