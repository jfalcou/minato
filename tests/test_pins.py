"""Test the pin rules of scripts/pins.py on small trees, against fixed tags and heads."""

import contextlib
import email.message
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import pins

CONFIG = {
    "owners": ["jfalcou"],
    "images": ["ghcr.io/jfalcou/compilers"],
    "matrices": {
        "files": ["**/.github/matrices/*.yml", "**/.github/matrices/*.yaml", "**/.github/matrices/*.json"],
        "registry": "ghcr.io/jfalcou/compilers",
    },
}
A, B, C, D, E = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
TAGS = {"jfalcou/tts": {"3.0": A, "4.0": B, "v4.0.0": B, "v4.1.0": C, "v4.2.0-rc1": D, "hpx-v9.0.0": D}}
IMAGES = ["v6", "v9", "v9b", "v10", "v11", "sycl-v1", "mpi-v1", "eve-v2", "eve-2026", "historic"]
TTS = "CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts GIT_TAG {})\n"


class Remote:
    """Answer for pins.Remote with fixed tags, heads, and image tags, without the network."""

    def __init__(self, tags=None, heads=None, head=C, images=None, errors=()):
        """Keep the answers.

        Args:
            tags: The tags of each repository, mapped to their commits.
            heads: The branches of each repository, `main` at the head by default.
            head: The commit of the default branch of every repository.
            images: The tags of every image.
            errors: The repositories and images that cannot be read.
        """
        self._tags = {k.lower(): v for k, v in (TAGS if tags is None else tags).items()}
        self._heads, self._head, self._errors = heads or {}, head, set(errors)
        self._images = IMAGES if images is None else images

    def _check(self, source):
        """Raise the error of a source that cannot be read.

        Args:
            source: The repository or the image.

        Raises:
            pins.SourceError: The source is one of the errors.
        """
        if source.lower() in self._errors:
            raise pins.SourceError(f"cannot read {source}")

    def tags(self, source):
        """Return the tags of a repository.

        Args:
            source: The repository, `owner/name`, in any case.

        Returns:
            Its tags, mapped to their commits.
        """
        self._check(source)
        return self._tags.get(source.lower(), {})

    def heads(self, source):
        """Return the branches of a repository.

        Args:
            source: The repository, `owner/name`, in any case.

        Returns:
            Its branches, `main` at the head by default.
        """
        self._check(source)
        return self._heads.get(source.lower(), {"main": self._head})

    def head(self, source):
        """Return the head of the default branch.

        Args:
            source: The repository, `owner/name`.

        Returns:
            The commit given to the constructor.
        """
        self._check(source)
        return self._head

    def image_tags(self, image):
        """Return the tags of an image.

        Args:
            image: The image.

        Returns:
            The tags given to the constructor.
        """
        self._check(image)
        return self._images


class Tree:
    """A clone made of a few files in a temporary directory."""

    def __init__(self, files):
        """Write the files.

        Args:
            files: The text of each file, by path, written without newline translation.
        """
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        for path, text in files.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            pins.write(self.root / path, text)

    def measure(self, remote=None, holds=()):
        """Return the resolved pins of the tree.

        Args:
            remote: The source of the tags, a Remote of this module by default.
            holds: The holds of the library.

        Returns:
            The pins.
        """
        return pins.measure(self.root, CONFIG, remote or Remote(), holds)

    def after(self, path, remote=None, holds=()):
        """Return the text of a file after the move.

        Args:
            path: The file, relative to the root of the tree.
            remote: The source of the tags, a Remote of this module by default.
            holds: The holds of the library.

        Returns:
            Its new text, or None when the move leaves it.
        """
        return pins.changes(self.root, self.measure(remote, holds)).get(path, (None, None))[1]


def summary(found):
    """Reduce pins to their current text, their new text, and their status.

    Args:
        found: The pins.

    Returns:
        One tuple per pin.
    """
    return [(p.text(False), p.text(True), p.status) for p in found]


class Case(unittest.TestCase):
    """A test case whose trees are removed at its end."""

    def tree(self, files):
        """Make a tree that the test case removes at its end.

        Args:
            files: The text of each file, by path.

        Returns:
            The tree.
        """
        tree = Tree(files)
        self.addCleanup(tree.dir.cleanup)
        return tree


class CMake(Case):
    """The GIT_TAG, the VERSION, and the `gh:` references of the CPM calls."""

    def test_bare_sha_moves_to_head_of_main(self):
        """A bare SHA, on one line or on the next of its call, moves to the head of main."""
        tree = self.tree(
            {
                "cmake/dependencies.cmake": "CPMAddPackage(NAME COPACABANA GITHUB_REPOSITORY jfalcou/copacabana"
                f" GIT_TAG {A})\n"
                f"CPMDeclarePackage ( TTS   NAME TTS   GITHUB_REPOSITORY jfalcou/tts\n"
                f"                    GIT_TAG {B}\n                  )\n"
            }
        )
        found = tree.measure(Remote(head=E))
        self.assertEqual(
            [(p.source, p.form, p.status, p.new) for p in found],
            [("jfalcou/copacabana", "sha", "behind", E), ("jfalcou/tts", "sha", "behind", E)],
        )
        self.assertEqual(
            tree.after("cmake/dependencies.cmake", Remote(head=E)).split("\n")[2], f"                    GIT_TAG {E}"
        )

    def test_sha_with_tag_comment_moves_to_the_newest_tag(self):
        """A SHA followed by the comment of its tag moves to the commit of the newest tag, and its comment follows."""
        tree = self.tree(
            {"deps.cmake": f"CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts\n  GIT_TAG {A} # 3.0\n)\n"}
        )
        self.assertEqual(tree.after("deps.cmake").split("\n")[1], f"  GIT_TAG {C} # v4.1.0")

    def test_comment_after_a_quote_or_a_parenthesis(self):
        """A tag comment after the closing quote of the SHA, or after the end of the call, is read."""
        tree = self.tree(
            {
                "deps.cmake": f'CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts GIT_TAG "{A}") # 3.0\n'
                f"CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts GIT_TAG {A}) # 3.0\n"
            }
        )
        self.assertEqual(
            tree.after("deps.cmake").split("\n")[:2],
            [
                f'CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts GIT_TAG "{C}") # v4.1.0',
                f"CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts GIT_TAG {C}) # v4.1.0",
            ],
        )

    def test_prose_comment_leaves_a_bare_sha(self):
        """A comment whose first word is no tag is prose: the SHA moves to the head and the comment stays."""
        tree = self.tree({"deps.cmake": TTS.format(A).rstrip("\n") + " # 2024 fix for #12\n"})
        self.assertEqual(tree.after("deps.cmake"), TTS.format(C).rstrip("\n") + " # 2024 fix for #12\n")

    def test_comment_naming_no_tag_is_unknown(self):
        """A comment shaped as a tag the source does not have leaves the pin unknown, never moved."""
        tree = self.tree({"deps.cmake": TTS.format(A).rstrip("\n") + " # v5.0.0\n"})
        [pin] = tree.measure()
        self.assertEqual((pin.form, pin.status), ("sha-tag", "unknown"))
        self.assertIn("v5.0.0", pin.error)
        self.assertIsNone(tree.after("deps.cmake"))

    def test_sha_that_is_not_the_commit_of_its_tag_is_unknown(self):
        """A SHA ahead of the tag its comment names stays where it is: moving it to the tag would go back."""
        tree = self.tree({"deps.cmake": TTS.format(E).rstrip("\n") + " # v4.1.0\n"})
        [pin] = tree.measure()
        self.assertEqual(pin.status, "unknown")
        self.assertIsNone(tree.after("deps.cmake"))

    def test_version_follows_the_tag(self):
        """The VERSION of a call takes the numbers of the tag its GIT_TAG moves to."""
        tree = self.tree(
            {"deps.cmake": "CPMFindPackage(NAME TTS VERSION 4.0\n  GITHUB_REPOSITORY jfalcou/tts GIT_TAG v4.0.0)\n"}
        )
        self.assertEqual(
            tree.after("deps.cmake"),
            "CPMFindPackage(NAME TTS VERSION 4.1.0\n  GITHUB_REPOSITORY jfalcou/tts GIT_TAG v4.1.0)\n",
        )

    def test_version_anchors_a_sha(self):
        """A VERSION beside a SHA names its tag: the SHA and the VERSION move together, or the pin is unknown."""
        tree = self.tree(
            {
                "a.cmake": f"CPMAddPackage(NAME TTS VERSION 3.0 GITHUB_REPOSITORY jfalcou/tts GIT_TAG {A})\n",
                "b.cmake": f"CPMAddPackage(NAME TTS VERSION 9.9 GITHUB_REPOSITORY jfalcou/tts GIT_TAG {A})\n",
            }
        )
        self.assertEqual(
            tree.after("a.cmake"), f"CPMAddPackage(NAME TTS VERSION 4.1.0 GITHUB_REPOSITORY jfalcou/tts GIT_TAG {C})\n"
        )
        self.assertEqual([p.status for p in tree.measure() if p.file == "b.cmake"], ["unknown"])

    def test_comments_branches_and_variables_stay(self):
        """A commented call is no pin, and a branch or a variable stays."""
        tree = self.tree(
            {
                "CMakeLists.txt": "## CPMDeclarePackage(TTS NAME TTS GITHUB_REPOSITORY jfalcou/tts GIT_TAG 4.0)\n"
                "CPMAddPackage(NAME EVE GITHUB_REPOSITORY jfalcou/eve GIT_TAG main)\n"
                'CPMAddPackage(NAME kumi GIT_REPOSITORY "https://github.com/jfalcou/kumi.git"'
                ' GIT_TAG "${GIT_BRANCH}")\n'
            }
        )
        self.assertEqual(
            [(p.source, p.form, p.status) for p in tree.measure()],
            [("jfalcou/eve", "branch", "fixed"), ("jfalcou/kumi", "variable", "fixed")],
        )

    def test_bracket_comments_and_escaped_quotes_hide_no_pin(self):
        """A call inside a bracket comment, or after an escaped quote, is no pin."""
        tree = self.tree(
            {
                "CMakeLists.txt": "#[==[\n" + TTS.format("3.0") + "]==]\n"
                'message(STATUS "say \\"hi")\n'
                "# " + TTS.format("3.0") + 'set(X [[ " ]])\n# ' + TTS.format("3.0")
            }
        )
        self.assertEqual(tree.measure(), [])

    def test_repository_after_the_tag_and_parenthesis_in_quotes(self):
        """GITHUB_REPOSITORY last in the call, and a parenthesis inside a quoted option, keep the pin."""
        tree = self.tree(
            {
                "CMakeLists.txt": "CPMAddPackage(NAME TTS GIT_TAG 3.0 GITHUB_REPOSITORY jfalcou/tts)\n"
                'CPMAddPackage(NAME TTS OPTIONS "X=a)b" GITHUB_REPOSITORY jfalcou/tts GIT_TAG 3.0)\n'
            }
        )
        self.assertEqual([(p.line, p.new) for p in tree.measure()], [(1, "v4.1.0"), (2, "v4.1.0")])

    def test_gh_shorthand(self):
        """The `gh:` form moves its version, or its SHA and version together, whatever the case of the owner."""
        tree = self.tree(
            {
                "CMakeLists.txt": 'CPMAddPackage("gh:jfalcou/tts@4.0.0")\n'
                f'CPMAddPackage("gh:JFalcou/tts#{A}@3.0")\n'
                'CPMAddPackage("gh:doctest/doctest@2.4.11")\n'
            }
        )
        self.assertEqual(
            tree.after("CMakeLists.txt"),
            f'CPMAddPackage("gh:jfalcou/tts@4.1.0")\nCPMAddPackage("gh:JFalcou/tts#{C}@4.1.0")\n'
            'CPMAddPackage("gh:doctest/doctest@2.4.11")\n',
        )

    def test_comment_tag_after_its_key_or_in_parentheses(self):
        """The tag of a comment is read wherever it sits in the comment, `tag=` and punctuation set apart."""
        tree = self.tree(
            {
                "a.cmake": TTS.format(B).rstrip("\n") + " # tag=v4.0.0\n",
                "b.cmake": TTS.format(B).rstrip("\n") + " # (v4.0.0)\n",
                "c.cmake": TTS.format(B).rstrip("\n") + " # v4.0.0, see #12\n",
            }
        )
        self.assertEqual(
            [tree.after(f) for f in ("a.cmake", "b.cmake", "c.cmake")],
            [
                TTS.format(C).rstrip("\n") + suffix
                for suffix in (" # tag=v4.1.0\n", " # (v4.1.0)\n", " # v4.1.0, see #12\n")
            ],
        )

    def test_tag_line_above_the_reference(self):
        """A comment line that holds a tag alone, right above the GIT_TAG, anchors its SHA."""
        tree = self.tree(
            {"deps.cmake": f"CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts\n  # v4.0.0\n  GIT_TAG {B})\n"}
        )
        self.assertEqual(
            tree.after("deps.cmake"),
            f"CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts\n  # v4.1.0\n  GIT_TAG {C})\n",
        )

    def test_escaped_quote_outside_quotes(self):
        """An escaped quote outside a quoted argument opens no quote, so the comment after it stays a comment."""
        tree = self.tree({"CMakeLists.txt": 'set(Q \\")\n# ' + TTS.format("4.0")})
        self.assertEqual(tree.measure(), [])

    def test_call_running_past_its_parenthesis(self):
        """A quote inside a bracket argument or escaped outside quotes ends no call early or late."""
        fmt = "CPMAddPackage(NAME fmt GITHUB_REPOSITORY fmtlib/fmt GIT_TAG v1.0.0)\n"
        tree = self.tree(
            {
                "a.cmake": 'CPMAddPackage(NAME TTS VERSION 4.0.0 GITHUB_REPOSITORY jfalcou/tts OPTIONS X=\\"-O2\\")\n'
                + fmt,
                "b.cmake": 'CPMAddPackage(NAME TTS VERSION 4.0.0 GITHUB_REPOSITORY jfalcou/tts OPTIONS [=[X="-O2]=])\n'
                + fmt,
            }
        )
        for path in ("a.cmake", "b.cmake"):
            first, second = tree.after(path).split("\n")[:2]
            self.assertIn("VERSION 4.1.0", first)
            self.assertEqual(second + "\n", fmt)

    def test_unclosed_call_takes_no_tag_of_the_next(self):
        """A call left without its closing parenthesis gives no pin, so the GIT_TAG of the next call stays its own."""
        fmt = "CPMAddPackage(NAME fmt GITHUB_REPOSITORY fmtlib/fmt GIT_TAG v1.0.0)\n"
        tree = self.tree({"CMakeLists.txt": "CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts\n" + fmt})
        self.assertEqual(tree.measure(), [])

    def test_version_alone_and_both_gh_orders(self):
        """A call with VERSION and no GIT_TAG, `@version#ref`, and an unquoted `gh:` are pins."""
        tree = self.tree(
            {
                "CMakeLists.txt": "CPMAddPackage(NAME TTS VERSION 4.0.0 GITHUB_REPOSITORY jfalcou/tts)\n"
                'CPMAddPackage("gh:jfalcou/tts@4.0.0#v4.0.0")\n'
                'CPMAddPackage(URI gh:jfalcou/tts@4.0.0 OPTIONS "X ON")\n'
            }
        )
        self.assertEqual(
            tree.after("CMakeLists.txt"),
            "CPMAddPackage(NAME TTS VERSION 4.1.0 GITHUB_REPOSITORY jfalcou/tts)\n"
            'CPMAddPackage("gh:jfalcou/tts@4.1.0#v4.1.0")\n'
            'CPMAddPackage(URI gh:jfalcou/tts@4.1.0 OPTIONS "X ON")\n',
        )

    def test_comment_of_a_gh_reference_or_a_version_alone(self):
        """The comment after a `gh:` reference or after a call with VERSION alone follows the move."""
        tree = self.tree(
            {
                "a.cmake": f'CPMAddPackage("gh:jfalcou/tts#{B}") # v4.0.0\n',
                "b.cmake": "CPMAddPackage(NAME TTS VERSION 4.0.0 GITHUB_REPOSITORY jfalcou/tts) # v4.0.0\n",
            }
        )
        self.assertEqual(tree.after("a.cmake"), f'CPMAddPackage("gh:jfalcou/tts#{C}") # v4.1.0\n')
        self.assertEqual(
            tree.after("b.cmake"), "CPMAddPackage(NAME TTS VERSION 4.1.0 GITHUB_REPOSITORY jfalcou/tts) # v4.1.0\n"
        )

    def test_brackets_inside_an_unquoted_argument(self):
        """A `[[` inside an unquoted argument opens no bracket argument, so comments and calls stay read as such."""
        fmt = "FetchContent_Declare(fmt GIT_REPOSITORY https://github.com/fmtlib/fmt.git GIT_TAG v1.0.0)\n"
        tree = self.tree(
            {
                "a.cmake": "if(NOT X MATCHES ^[[:alnum:]_]+$)\nendif()\n# " + TTS.format("3.0") + TTS.format("4.0"),
                "b.cmake": "CPMAddPackage(NAME TTS VERSION 4.0.0 GITHUB_REPOSITORY jfalcou/tts OPTIONS X=a[[b)\n" + fmt,
            }
        )
        self.assertEqual([(p.file, p.line) for p in tree.measure()], [("a.cmake", 4), ("b.cmake", 1)])
        self.assertEqual(tree.after("b.cmake").split("\n")[1] + "\n", fmt)

    def test_url_form(self):
        """The single argument `https://github.com/owner/name.git@version#ref` of CPM is a pin."""
        tree = self.tree({"CMakeLists.txt": 'CPMAddPackage("https://github.com/jfalcou/tts.git@4.0.0#v4.0.0")\n'})
        self.assertEqual(
            tree.after("CMakeLists.txt"), 'CPMAddPackage("https://github.com/jfalcou/tts.git@4.1.0#v4.1.0")\n'
        )

    def test_key_of_a_tag_comment_in_any_spelling(self):
        """`Tag=`, `tag:`, and `version` before the tag of a comment, or of the line above, are set apart."""
        tree = self.tree(
            {
                "a.cmake": TTS.format(B).rstrip("\n") + " # Tag=v4.0.0\n",
                "b.cmake": TTS.format(B).rstrip("\n") + " # tag: v4.0.0\n",
                "c.cmake": TTS.format(B).rstrip("\n") + " # version v4.0.0\n",
                "d.cmake": "# Tag: v4.0.0\n" + TTS.format(B),
            }
        )
        self.assertEqual(
            [tree.after(f) for f in ("a.cmake", "b.cmake", "c.cmake", "d.cmake")],
            [TTS.format(C).rstrip("\n") + s for s in (" # Tag=v4.1.0\n", " # tag: v4.1.0\n", " # version v4.1.0\n")]
            + ["# Tag: v4.1.0\n" + TTS.format(C)],
        )

    def test_other_owners_are_not_pins(self):
        """A repository of another owner is left to Dependabot."""
        tree = self.tree(
            {
                "CMakeLists.txt": "CPMAddPackage(NAME doctest GITHUB_REPOSITORY doctest/doctest GIT_TAG v2.4.11)\n",
                ".github/workflows/ci.yml": "    steps:\n      - uses: actions/checkout@v7\n",
            }
        )
        self.assertEqual(tree.measure(), [])


class Tags(Case):
    """The families of the repository tags, and the holds."""

    def test_old_form_moves_to_the_newest_vxyz_tag(self):
        """`4.0` and `v4.1.0` share a family, so `4.0` moves to `v4.1.0`."""
        [pin] = self.tree({"CMakeLists.txt": TTS.format("4.0")}).measure()
        self.assertEqual((pin.form, pin.new, pin.status), ("tag", "v4.1.0", "behind"))

    def test_same_version_takes_its_vxyz_name(self):
        """Of two tags on one release, the pin takes the `vX.Y.Z` name, once."""
        remote = Remote(tags={"jfalcou/tts": {"4.0": B, "v4.0.0": B}})
        tree = self.tree({"a.cmake": TTS.format("4.0"), "b.cmake": TTS.format("v4.0.0")})
        self.assertEqual(summary(tree.measure(remote)), [("4.0", "v4.0.0", "behind"), ("v4.0.0", "v4.0.0", "current")])

    def test_rename_only_keeps_the_commit(self):
        """A SHA on the newest release takes the `vX.Y.Z` name in its comment and keeps its commit."""
        remote = Remote(tags={"jfalcou/copacabana": {"v8": A, "v8.0.0": A}})
        tree = self.tree(
            {
                ".github/workflows/ci.yml": f"    uses: jfalcou/copacabana/.github/workflows/m.yml@{A} # v8\n"
                f"    uses: jfalcou/copacabana/.github/workflows/m.yml@{A} # v8.0.0\n"
            }
        )
        self.assertEqual(
            summary(tree.measure(remote)),
            [(f"{A} # v8", f"{A} # v8.0.0", "behind"), (f"{A} # v8.0.0", f"{A} # v8.0.0", "current")],
        )

    def test_prerelease_and_other_families_are_never_targets(self):
        """A pre-release and a tag of another prefix never become the newest tag of a family."""
        tree = self.tree(
            {
                ".github/workflows/ci.yml": "      - uses: jfalcou/tts/.github/workflows/x.yml@v4.1.0\n"
                "      - uses: jfalcou/tts/.github/workflows/x.yml@hpx-v9.0.0\n"
            }
        )
        self.assertEqual(
            summary(tree.measure()), [("v4.1.0", "v4.1.0", "current"), ("hpx-v9.0.0", "hpx-v9.0.0", "current")]
        )

    def test_uses_sha_with_tag_comment(self):
        """A `uses:` on a SHA with its tag comment moves both, quoted or not."""
        tree = self.tree(
            {
                ".github/workflows/ci.yml": f"    uses: jfalcou/tts/.github/workflows/x.yml@{A} # 3.0\n"
                f"    uses: 'jfalcou/tts/.github/workflows/x.yml@{A}' # 3.0\n"
            }
        )
        self.assertEqual(
            tree.after(".github/workflows/ci.yml"),
            f"    uses: jfalcou/tts/.github/workflows/x.yml@{C} # v4.1.0\n"
            f"    uses: 'jfalcou/tts/.github/workflows/x.yml@{C}' # v4.1.0\n",
        )

    def test_tag_outside_every_family_stays(self):
        """A tag that is no version, `stable`, stays."""
        remote = Remote(tags={"jfalcou/tts": {"stable": A}})
        [pin] = self.tree({"CMakeLists.txt": TTS.format("stable")}).measure(remote)
        self.assertEqual((pin.form, pin.status), ("fixed", "fixed"))

    def test_missing_tag_and_missing_head_are_unknown(self):
        """A tag the source does not have, and a bare SHA of a source without default branch, are unknown."""
        tree = self.tree({"a.cmake": TTS.format("v9.9.9"), "b.cmake": TTS.format(A)})
        self.assertEqual([p.status for p in tree.measure(Remote(head=""))], ["unknown", "unknown"])

    def test_held_pin_stays(self):
        """A pin under a hold is marked held, and its file is left as it is."""
        tree = self.tree({"tools/template/cmake/dependencies.cmake": TTS.format("4.0")})
        holds = [{"source": "jfalcou/tts", "files": ["tools/template/**"], "reason": "test"}]
        [pin] = tree.measure(holds=holds)
        self.assertEqual(pin.status, "held")
        self.assertIsNone(tree.after("tools/template/cmake/dependencies.cmake", holds=holds))

    def test_hold_without_files_and_hold_on_an_image(self):
        """A hold without globs covers every file, and a hold may name an image."""
        tree = self.tree(
            {
                "CMakeLists.txt": TTS.format("4.0"),
                ".github/workflows/ci.yml": "      image: ghcr.io/jfalcou/compilers:v10\n",
            }
        )
        holds = [{"source": "jfalcou/tts", "reason": "a"}, {"source": "ghcr.io/jfalcou/compilers", "reason": "b"}]
        self.assertEqual([p.status for p in tree.measure(holds=holds)], ["held", "held"])

    def test_prose_naming_another_release_stays(self):
        """A comment on a tag pin that names another release is prose, and the move leaves it."""
        remote = Remote(tags={"jfalcou/copacabana": {"v7.0.0": A, "v8": B, "v8.0.0": B}})
        tree = self.tree(
            {
                "ci.yml": "    uses: jfalcou/copacabana/.github/workflows/ci.yml@v7.0.0 # v8 drops the jobs input\n",
                "a.cmake": TTS.format("v4.0.0").replace(")\n", ") # 3.0 broke the build\n"),
            }
        )
        self.assertEqual(
            tree.after("ci.yml", remote),
            "    uses: jfalcou/copacabana/.github/workflows/ci.yml@v8.0.0 # v8 drops the jobs input\n",
        )
        self.assertEqual(tree.after("a.cmake"), TTS.format("v4.1.0").replace(")\n", ") # 3.0 broke the build\n"))

    def test_hold_matches_any_case(self):
        """A hold names its source in any case, as the owners of minato.json do."""
        tree = self.tree({"CMakeLists.txt": TTS.format("4.0").replace("jfalcou/tts", "JFalcou/tts")})
        [pin] = tree.measure(holds=[{"source": "jfalcou/tts", "reason": "test"}])
        self.assertEqual(pin.status, "held")

    def test_numbers_in_prose_anchor_nothing(self):
        """Only the first word of a comment can name the tag, so a number later in the prose leaves a bare SHA."""
        tree = self.tree(
            {
                ".github/workflows/ci.yml": f"    uses: jfalcou/tts/.github/workflows/x.yml@{A} # needs 4.0 or later\n"
                f"    uses: jfalcou/tts/.github/workflows/x.yml@{A} # main until v5 ships\n"
            }
        )
        self.assertEqual([(p.form, p.status, p.new) for p in tree.measure()], [("sha", "behind", C)] * 2)

    def test_tag_line_inside_a_block_scalar(self):
        """The last line of a `run: |` script is no tag line for the `uses:` that follows the block."""
        tree = self.tree(
            {
                ".github/workflows/ci.yml": "      - run: |\n          make\n          # v4.0.0\n"
                f"      - uses: jfalcou/tts/.github/workflows/x.yml@{B}\n"
            }
        )
        [pin] = tree.measure()
        self.assertEqual((pin.form, pin.new), ("sha", C))
        self.assertIn("          # v4.0.0\n", tree.after(".github/workflows/ci.yml"))

    def test_block_scalar_ends_at_a_line_indented_less(self):
        """A comment indented less than the content of a block scalar ends it, and is the tag line of the next pin."""
        tree = self.tree(
            {
                ".github/workflows/ci.yml": "      - run: |\n            make\n          # v4.0.0\n"
                f"      - uses: jfalcou/tts/.github/workflows/x.yml@{B}\n"
            }
        )
        self.assertEqual(
            tree.after(".github/workflows/ci.yml"),
            "      - run: |\n            make\n          # v4.1.0\n"
            f"      - uses: jfalcou/tts/.github/workflows/x.yml@{C}\n",
        )

    def test_version_prose_and_key_prefixed_tags(self):
        """`version` before a number is prose, and a key glued to a tag by `-` is part of the word."""
        tree = self.tree(
            {
                ".github/workflows/a.yml": f"    uses: jfalcou/tts/.github/workflows/x.yml@{E}"
                " # version 4.0 is the minimum\n",
                ".github/workflows/b.yml": f"    # Version 4.0\n    uses: jfalcou/tts/.github/workflows/x.yml@{E}\n",
                ".github/workflows/c.yml": f"    uses: jfalcou/tts/.github/workflows/x.yml@{B} # tag-v4.0.0\n",
            }
        )
        self.assertEqual(
            [(p.file, p.form, p.status) for p in tree.measure()],
            [
                (".github/workflows/a.yml", "sha", "behind"),
                (".github/workflows/b.yml", "sha", "behind"),
                (".github/workflows/c.yml", "sha-tag", "unknown"),
            ],
        )

    def test_block_scalar_with_an_indentation_indicator(self):
        """Under `|2`, the content starts two columns after its key, whatever the indentation of its first line."""
        tree = self.tree(
            {
                ".github/workflows/ci.yml": "      - run: |2\n"
                "              echo first\n"
                "            uses: jfalcou/tts/.github/workflows/x.yml@3.0\n"
                "      - uses: jfalcou/tts/.github/workflows/x.yml@3.0\n"
            }
        )
        self.assertEqual([p.line for p in tree.measure()], [4])


class Images(Case):
    """The tags of the compilers image, in full references, matrices, and Dockerfiles."""

    def test_full_references_follow_their_family(self):
        """`v10` moves to `v11`, `mpi-v1` stays in its family, and the tags of no family stay."""
        tree = self.tree(
            {
                ".github/workflows/ci.yml": "    container:\n      image: ghcr.io/jfalcou/compilers:v10\n"
                "      image: ghcr.io/jfalcou/compilers:mpi-v1\n"
                "      image: ghcr.io/jfalcou/compilers:v9b\n"
                "      image: ghcr.io/jfalcou/compilers:eve-2026\n"
                "      image: ghcr.io/jfalcou/eve:latest\n"
            }
        )
        self.assertEqual(
            summary(tree.measure()),
            [
                ("v10", "v11", "behind"),
                ("mpi-v1", "mpi-v1", "current"),
                ("v9b", "v9b", "fixed"),
                ("eve-2026", "eve-2026", "fixed"),
            ],
        )

    def test_tag_ahead_of_the_registry_is_unknown(self):
        """A tag the registry does not list yet stays, in a full reference as in a matrix."""
        tree = self.tree(
            {
                ".github/workflows/ci.yml": "      image: ghcr.io/jfalcou/compilers:v12\n",
                ".github/matrices/linux.yml": "image: v12\n",
            }
        )
        self.assertEqual([p.status for p in tree.measure()], ["unknown", "unknown"])
        self.assertEqual(pins.changes(tree.root, tree.measure()), {})

    def test_matrix_short_tags(self):
        """The short `image:` of a matrix moves, at the top of the file and in a row, comments excepted."""
        tree = self.tree(
            {
                ".github/matrices/linux.yml": "##   image: v6\nruns-on: ubuntu-latest\nimage: v10\nrows:\n"
                '  - { name: "icpx", preset: "icpx", image: "sycl-v1", setup: "x.sh" }\n'
                '  - { name: "gcc" , preset: "gcc" , image: "v9"    }\n'
            }
        )
        self.assertEqual(
            [(p.line, p.ref, p.new) for p in tree.measure()],
            [(3, "v10", "v11"), (5, "sycl-v1", "sycl-v1"), (6, "v9", "v11")],
        )
        self.assertIn('image: "v11"    }', tree.after(".github/matrices/linux.yml"))

    def test_registry_key_read_as_yaml(self):
        """A `registry` with a comment or a space before its colon is read, and so is a quoted one."""
        tree = self.tree(
            {
                ".github/matrices/a.yml": "registry: ghcr.io/other/cuda # CUDA images\nimage: v2\n",
                ".github/matrices/b.yml": "registry : ghcr.io/other/img\nimage: v2\n",
                ".github/matrices/c.yml": "registry: 'ghcr.io/jfalcou/compilers'\nimage: v10\n",
            }
        )
        self.assertEqual([(p.file, p.new) for p in tree.measure()], [(".github/matrices/c.yml", "v11")])

    def test_matrix_naming_compilers_registry(self):
        """A matrix that names the compilers registry itself is read."""
        tree = self.tree({".github/matrices/linux.yml": "registry: ghcr.io/jfalcou/compilers\nimage: v10\n"})
        self.assertEqual([p.new for p in tree.measure()], ["v11"])

    def test_comments_blocks_and_other_keys_hide_no_pin(self):
        """A trailing comment, a block scalar, and a key that ends in `image` hold no pin."""
        tree = self.tree(
            {
                ".github/workflows/ci.yml": "      image: ghcr.io/jfalcou/compilers:v11 # v9 failed, see"
                " ghcr.io/jfalcou/compilers:v9\n"
                "      - run: |\n"
                "          uses: jfalcou/tts/.github/workflows/x.yml@3.0\n"
                "          echo ghcr.io/jfalcou/compilers:v9\n"
                "        shell: bash\n"
                "      - uses: jfalcou/tts/.github/workflows/x.yml@3.0\n",
                ".github/matrices/linux.yml": "image: v10\nrows:\n"
                '  - { name: "a", docker-image: v10, image: v10 } # image: v10 broke icpx\n',
            }
        )
        self.assertEqual(
            [(p.file, p.line, p.ref) for p in tree.measure()],
            [
                (".github/matrices/linux.yml", 1, "v10"),
                (".github/matrices/linux.yml", 3, "v10"),
                (".github/workflows/ci.yml", 1, "v11"),
                (".github/workflows/ci.yml", 6, "3.0"),
            ],
        )
        self.assertEqual(
            tree.after(".github/matrices/linux.yml").split("\n")[2],
            '  - { name: "a", docker-image: v10, image: v11 } # image: v10 broke icpx',
        )

    def test_single_quotes_and_yaml_and_json_matrices(self):
        """A single-quoted tag, a `.yaml` matrix, and a `.json` matrix are read."""
        tree = self.tree(
            {
                ".github/matrices/a.yaml": "image: 'v10'\n",
                ".github/matrices/b.json": '{"image": "v10", "rows": [{"name": "x", "image": "sycl-v1"}]}\n',
            }
        )
        self.assertEqual(
            summary(tree.measure()),
            [("v10", "v11", "behind"), ("v10", "v11", "behind"), ("sycl-v1", "sycl-v1", "current")],
        )
        self.assertEqual(tree.after(".github/matrices/a.yaml"), "image: 'v11'\n")

    def test_dockerfile_from(self):
        """The FROM of a Dockerfile is a pin, its comment lines are not."""
        tree = self.tree(
            {"docker/Dockerfile": "# FROM ghcr.io/jfalcou/compilers:v9\nFROM ghcr.io/jfalcou/compilers:v10 AS base\n"}
        )
        self.assertEqual([(p.line, p.new) for p in tree.measure()], [(2, "v11")])

    def test_template_matrices_are_read(self):
        """A matrix below the root, the one of tools/template, is read too."""
        tree = self.tree({"tools/template/.github/matrices/linux.yml": "image: v10\n"})
        self.assertEqual([p.new for p in tree.measure()], ["v11"])

    def test_digest_is_unknown(self):
        """An image pinned by digest stays: a new tag beside the old digest would still pull the old image."""
        tree = self.tree({"Dockerfile": "FROM ghcr.io/jfalcou/compilers:v10@sha256:" + "0" * 64 + "\n"})
        [pin] = tree.measure()
        self.assertEqual((pin.form, pin.status), ("digest", "unknown"))
        self.assertIsNone(tree.after("Dockerfile"))

    def test_dockerfile_names(self):
        """`Dockerfile.dev` is a Dockerfile, `Dockerfile.md` a document."""
        line = "FROM ghcr.io/jfalcou/compilers:v10\n"
        tree = self.tree(
            {"docker/Dockerfile.dev": line, "docker/Dockerfile.md": "Starts from `" + line.strip() + "`.\n"}
        )
        self.assertEqual([p.file for p in tree.measure()], ["docker/Dockerfile.dev"])

    def test_quote_inside_a_plain_scalar(self):
        """A quote inside a plain scalar opens no quoted scalar, so the comment after it stays a comment."""
        tree = self.tree({".github/workflows/ci.yml": "      run: echo 'x # ghcr.io/jfalcou/compilers:v9\n"})
        self.assertEqual(tree.measure(), [])

    def test_pyyaml_only_for_matrices(self):
        """Without PyYAML, a clone without matrix is measured, and a matrix file stops with its reason."""
        plain = self.tree({"CMakeLists.txt": TTS.format("4.0")})
        matrix = self.tree({".github/matrices/linux.yml": "image: v10\n"})
        with mock.patch.dict(sys.modules, {"yaml": None}):
            self.assertEqual([p.status for p in plain.measure()], ["behind"])
            with self.assertRaisesRegex(pins.LibraryError, "PyYAML"):
                matrix.measure()

    def test_compact_json_matrix(self):
        """A JSON string right after its colon is a string, so a `#` inside it hides nothing."""
        tree = self.tree({".github/matrices/linux.json": '{"rows":[{"preset":"gcc","setup":"a #1","image":"v10"}]}\n'})
        self.assertEqual([p.new for p in tree.measure()], ["v11"])

    def test_dockerfile_backup_and_notes(self):
        """A backup of a Dockerfile, and a file named as one without a FROM instruction, are not read."""
        line = "FROM ghcr.io/jfalcou/compilers:v10\n"
        tree = self.tree(
            {
                "docker/Dockerfile.bak": line,
                "docker/Dockerfile.notes": "Based on ghcr.io/jfalcou/compilers:v10.\n",
                "docker/Dockerfile": line,
            }
        )
        self.assertEqual([p.file for p in tree.measure()], ["docker/Dockerfile"])

    def test_quote_after_a_colon_in_a_plain_scalar(self):
        """Outside a flow collection, a quote right after `:` is a character, so the comment after it stays one."""
        tree = self.tree({".github/workflows/ci.yml": "      - run: echo a:'b # was ghcr.io/jfalcou/compilers:v9\n"})
        self.assertEqual(tree.measure(), [])

    def test_dockerfile_with_a_byte_order_mark(self):
        """A Dockerfile that opens with a byte order mark is read, and keeps the mark."""
        tree = self.tree({"Dockerfile": "\ufeffFROM ghcr.io/jfalcou/compilers:v10\n"})
        self.assertEqual(tree.after("Dockerfile"), "\ufeffFROM ghcr.io/jfalcou/compilers:v11\n")

    def test_bracket_inside_a_plain_scalar(self):
        """A `[` inside a plain scalar opens no flow collection, so a quote after `:` there opens nothing."""
        tree = self.tree(
            {".github/workflows/ci.yml": "      - run: echo [WARN a:'b # was ghcr.io/jfalcou/compilers:v9\n"}
        )
        self.assertEqual(tree.measure(), [])

    def test_flow_row_after_a_colon(self):
        """A flow collection right after a colon, `"rows":[{...}]`, is still read as JSON."""
        tree = self.tree(
            {
                ".github/matrices/linux.yml": 'image: v10\nrows: [{"preset":"gcc","setup":"x #1","image":"v10"}]\n',
                ".github/matrices/b.json": '{"rows":[{"setup":"y #2","image":"v10"}]}\n',
            }
        )
        self.assertEqual(
            [(p.file, p.line, p.new) for p in tree.measure()][-2:],
            [
                (".github/matrices/linux.yml", 1, "v11"),
                (".github/matrices/linux.yml", 2, "v11"),
            ],
        )
        self.assertEqual(len(tree.measure()), 3)


class Files(Case):
    """The files a clone offers, and their rewrite."""

    def test_crlf_is_kept(self):
        """A file with CRLF line endings keeps them once a pin moves."""
        tree = self.tree({"CMakeLists.txt": "project(x)\r\n" + TTS.format("4.0").replace("\n", "\r\n")})
        self.assertEqual(tree.after("CMakeLists.txt"), "project(x)\r\n" + TTS.format("v4.1.0").replace("\n", "\r\n"))

    def test_symlink_is_skipped(self):
        """A tracked symbolic link is not followed, so no file outside the clone is rewritten."""
        outside = self.tree({"deps.cmake": TTS.format("4.0")})
        tree = self.tree({"x.txt": ""})
        os.symlink(outside.root / "deps.cmake", tree.root / "deps.cmake")
        self.assertEqual(tree.measure(), [])

    def test_work_tree_lists_its_tracked_files(self):
        """At the top of a work tree, the tracked files alone are read."""
        tree = self.tree({"a.cmake": TTS.format("4.0"), "b.cmake": TTS.format("4.0")})
        subprocess.run(["git", "init", "-q", str(tree.root)], check=True)
        subprocess.run(["git", "-C", str(tree.root), "add", "a.cmake"], check=True)
        self.assertEqual(pins.files(tree.root), ["a.cmake"])

    def test_nested_tree_in_a_work_tree(self):
        """A tree inside another work tree is walked, its files untracked there."""
        outer = self.tree({"README": ""})
        subprocess.run(["git", "init", "-q", str(outer.root)], check=True)
        (outer.root / "clones" / "kumi").mkdir(parents=True)
        pins.write(outer.root / "clones" / "kumi" / "CMakeLists.txt", TTS.format("4.0"))
        self.assertEqual(pins.files(outer.root / "clones" / "kumi"), ["CMakeLists.txt"])

    def test_moved_refuses_a_pin_out_of_place(self):
        """A pin whose text no longer sits at its column stops the rewrite."""
        pin = pins.Pin("cpm", "x", 1, "jfalcou/tts", "4.0", 3, new="v4.1.0", status="behind")
        with self.assertRaises(ValueError):
            pins.moved("GIT_TAG 4.0\n", [pin])

    def test_apply_is_idempotent(self):
        """A second measure after the move finds nothing behind."""
        tree = self.tree({"CMakeLists.txt": TTS.format(A), "ci.yml": "image: ghcr.io/jfalcou/compilers:v10\n"})
        for path, (_, after) in pins.changes(tree.root, tree.measure()).items():
            pins.write(tree.root / path, after)
        self.assertEqual([p.status for p in tree.measure()], ["current", "current"])


class Remotes(unittest.TestCase):
    """The reading of `git ls-remote` and of the registry, and their failures."""

    def test_annotated_tags_take_their_commit(self):
        """An annotated tag maps to the commit it tags, and HEAD to the head of the default branch."""
        out = (
            f"ref: refs/heads/main\tHEAD\n{A}\tHEAD\n{A}\trefs/heads/main\n"
            f"{B}\trefs/tags/v1\n{C}\trefs/tags/v1^{{}}\n{D}\trefs/tags/v2\n"
        )
        done = subprocess.CompletedProcess([], 0, stdout=out)
        with mock.patch.object(pins.subprocess, "run", return_value=done):
            remote = pins.Remote()
            self.assertEqual(
                (remote.head("x/y"), remote.heads("x/y"), remote.tags("x/y")), (A, {"main": A}, {"v1": C, "v2": D})
            )

    def test_git_never_prompts_and_times_out(self):
        """A git command runs without a terminal prompt and under a timeout."""
        done = subprocess.CompletedProcess([], 0, stdout="")
        with mock.patch.object(pins.subprocess, "run", return_value=done) as run:
            pins.Remote().tags("x/y")
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(run.call_args.kwargs["timeout"], pins.TIMEOUT)

    def test_git_failure_carries_its_message_once(self):
        """A repository git cannot read raises its message, and is not read twice."""
        failure = subprocess.CalledProcessError(128, ["git"], stderr="fatal: repository not found\n")
        with mock.patch.object(pins.subprocess, "run", side_effect=failure) as run:
            remote = pins.Remote()
            for _ in range(2):
                with self.assertRaisesRegex(pins.SourceError, "repository not found"):
                    remote.tags("x/y")
        self.assertEqual(run.call_count, 1)

    def test_unreadable_source_is_unknown(self):
        """A source that cannot be read leaves its pins unknown, and the other pins are measured."""
        tree = Tree(
            {
                "CMakeLists.txt": TTS.format("4.0")
                + "CPMAddPackage(NAME C GITHUB_REPOSITORY jfalcou/copacabana GIT_TAG main)\n"
            }
        )
        self.addCleanup(tree.dir.cleanup)
        found = tree.measure(Remote(errors={"jfalcou/tts"}))
        self.assertEqual([(p.status, p.error) for p in found], [("unknown", "cannot read jfalcou/tts"), ("fixed", "")])

    def test_registry_pages_and_token(self):
        """The registry is read with the token of its challenge, page after page."""

        def reply(body, link=None):
            """Make an answer of the registry.

            Args:
                body: The JSON body.
                link: The Link header, when a next page follows.

            Returns:
                A response that json.load reads and whose headers name the link.
            """
            answer = io.BytesIO(json.dumps(body).encode())
            answer.headers = email.message.Message()
            if link:
                answer.headers["Link"] = link
            return answer

        challenge = email.message.Message()
        challenge["WWW-Authenticate"] = 'Bearer realm="https://r.io/token",service="r.io"'
        steps = [
            urllib.error.HTTPError("https://r.io/v2/o/i/tags/list?n=1000", 401, "", challenge, None),
            reply({"token": "t"}),
            reply({"tags": ["v1"]}, '</v2/o/i/tags/list?last=v1&n=1000>; rel="next"'),
            reply({"tags": ["v2"]}),
        ]
        with mock.patch.object(pins.urllib.request, "urlopen", side_effect=steps) as urlopen:
            self.assertEqual(pins.Remote().image_tags("r.io/o/i"), ["v1", "v2"])
        self.assertIn("scope=repository%3Ao%2Fi%3Apull", urlopen.call_args_list[1].args[0])
        self.assertEqual(urlopen.call_args_list[3].args[0].full_url, "https://r.io/v2/o/i/tags/list?last=v1&n=1000")

    def test_registry_failure_is_a_source_error(self):
        """A registry that refuses the request raises a SourceError."""
        refusal = urllib.error.HTTPError("u", 429, "Too Many Requests", email.message.Message(), None)
        with (
            mock.patch.object(pins.urllib.request, "urlopen", side_effect=refusal),
            self.assertRaisesRegex(pins.SourceError, "429"),
        ):
            pins.Remote().image_tags("r.io/o/i")


class Commands(Case):
    """The commands, against a minato tree of their own."""

    def setUp(self):
        """Make a minato tree with tts updated and eve frozen, and a clone of tts."""
        self.minato = self.tree(
            {
                "minato.json": json.dumps(CONFIG),
                "libraries/tts/library.json": '{"repository": "jfalcou/tts", "updates": true}',
                "libraries/eve/library.json": '{"repository": "jfalcou/eve", "updates": false}',
            }
        )
        self.clones = self.tree({"tts/CMakeLists.txt": TTS.format("4.0")})
        for patch in (mock.patch.object(pins, "ROOT", self.minato.root), mock.patch.object(pins, "Remote", Remote)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, *argv):
        """Run the script and return its exit status and its output."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = pins.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def test_apply_moves_and_reports(self):
        """`apply` rewrites the clone and prints the pin it moved."""
        clone = self.clones.root / "tts"
        status, out, _ = self.run_main("apply", "tts", str(clone))
        self.assertEqual((status, out), (0, "CMakeLists.txt:1: jfalcou/tts 4.0 -> v4.1.0\n"))
        self.assertEqual(pins.read(clone / "CMakeLists.txt"), TTS.format("v4.1.0"))

    def test_apply_refuses_unknown_and_frozen_libraries(self):
        """`apply` refuses a library minato does not list and one it does not update, and writes nothing."""
        clone = self.clones.root / "tts"
        self.assertEqual(self.run_main("apply", "Tts", str(clone))[0], 2)
        self.assertEqual(self.run_main("apply", "eve", str(clone))[0], 2)
        self.assertEqual(pins.read(clone / "CMakeLists.txt"), TTS.format("4.0"))

    def test_check_goes_on_past_a_library_error(self):
        """`check` reports a library without clone, measures the others, writes their pins.json, and fails."""
        status, out, err = self.run_main("check", "--clones", str(self.clones.root), "--write")
        self.assertEqual(status, 1)
        self.assertIn("== eve: no clone", err)
        self.assertIn("== tts: 1 behind", out)
        written = json.loads((self.minato.root / "libraries/tts/pins.json").read_text())
        self.assertEqual([p["latest"] for p in written["pins"]], ["v4.1.0"])

    def test_malformed_holds_are_reported(self):
        """A holds.json without source stops that library alone, with exit status 1 for check and 2 for apply."""
        pins.write(self.minato.root / "libraries/tts/holds.json", '[{"files": ["x"]}]')
        status, _, err = self.run_main("check", "--clones", str(self.clones.root))
        self.assertEqual(status, 1)
        self.assertIn("holds.json", err)
        self.assertEqual(self.run_main("apply", "tts", str(self.clones.root / "tts"))[0], 2)


if __name__ == "__main__":
    unittest.main()
