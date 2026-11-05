"""Test the pin rules of scripts/pins.py on small trees, against fixed tags and heads."""

import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import pins

CONFIG = {
    "owners": ["jfalcou"],
    "images": ["ghcr.io/jfalcou/compilers"],
    "matrices": {"files": "**/.github/matrices/*.yml", "registry": "ghcr.io/jfalcou/compilers"},
}
A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
TAGS = {"jfalcou/tts": {"3.0": A, "4.0": B, "v4.0.0": B, "v4.1.0": C, "v4.2.0-rc1": D, "hpx-v9.0.0": D}}
IMAGES = ["v6", "v9", "v9b", "v10", "v11", "sycl-v1", "mpi-v1", "eve-v2", "eve-2026", "historic"]


class Remote:
    """Answer for pins.Remote with fixed tags, heads, and image tags, without the network."""

    def __init__(self, tags=None, heads=None, head="", images=None):
        """Keep the answers.

        Args:
            tags: The tags of each repository, mapped to their commits.
            heads: The branches of each repository, `main` at the head by default.
            head: The commit of the default branch of every repository.
            images: The tags of every image.
        """
        self._tags, self._heads, self._head, self._images = tags or {}, heads or {}, head, images or []

    def tags(self, source):
        """Return the tags of a repository."""
        return self._tags.get(source, {})

    def heads(self, source):
        """Return the branches of a repository."""
        return self._heads.get(source, {"main": self._head})

    def head(self, source):
        """Return the commit of the default branch."""
        return self._head

    def image_tags(self, image):
        """Return the tags of an image."""
        return self._images


class Tree:
    """A clone made of a few files in a temporary directory."""

    def __init__(self, files):
        """Write the files.

        Args:
            files: The text of each file, by path.
        """
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        for path, text in files.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text)

    def measure(self, remote, holds=()):
        """Return the resolved pins of the tree."""
        return pins.measure(self.root, CONFIG, remote, holds)

    def after(self, remote, path, holds=()):
        """Return the text of a file after the move, or None when the move leaves it."""
        return pins.changes(self.root, self.measure(remote, holds)).get(path, (None, None))[1]


class Case(unittest.TestCase):
    """A test case whose trees are removed at its end."""

    def tree(self, files):
        """Make a tree that the test case removes at its end."""
        tree = Tree(files)
        self.addCleanup(tree.dir.cleanup)
        return tree


class CMake(Case):
    """The GIT_TAG of the CPM calls."""

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
        found = tree.measure(Remote(head=C))
        self.assertEqual(
            [(p.source, p.form, p.status, p.latest) for p in found],
            [("jfalcou/copacabana", "sha", "behind", C), ("jfalcou/tts", "sha", "behind", C)],
        )
        after = tree.after(Remote(head=C), "cmake/dependencies.cmake")
        self.assertEqual(after.split("\n")[2], f"                    GIT_TAG {C}")

    def test_sha_with_tag_comment_moves_to_the_newest_tag(self):
        """A SHA followed by `# vX.Y.Z` moves to the commit of the newest tag, and its comment follows."""
        tree = self.tree(
            {
                "cmake/dependencies.cmake": "CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts\n"
                f"  GIT_TAG {A} # v4.0.0\n)\n"
            }
        )
        remote = Remote(tags={"jfalcou/tts": {"4.0": A, "v4.0.0": A, "v4.1.0": B}}, head=C)
        self.assertEqual(tree.after(remote, "cmake/dependencies.cmake").split("\n")[1], f"  GIT_TAG {B} # v4.1.0")

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
        found = tree.measure(Remote(head=C))
        self.assertEqual(
            [(p.source, p.form, p.status) for p in found],
            [("jfalcou/eve", "branch", "fixed"), ("jfalcou/kumi", "variable", "fixed")],
        )

    def test_other_owners_are_not_pins(self):
        """A repository of another owner is left to Dependabot."""
        tree = self.tree(
            {
                "CMakeLists.txt": "CPMAddPackage(NAME doctest GITHUB_REPOSITORY doctest/doctest GIT_TAG v2.4.11)\n",
                ".github/workflows/ci.yml": "    steps:\n      - uses: actions/checkout@v7\n",
            }
        )
        self.assertEqual(tree.measure(Remote()), [])


class Tags(Case):
    """The families of the repository tags."""

    def test_old_form_moves_to_the_newest_vxyz_tag(self):
        """`4.0` and `v4.1.0` share a family, so `4.0` moves to `v4.1.0`."""
        tree = self.tree({"CMakeLists.txt": "CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts GIT_TAG 4.0)\n"})
        [pin] = tree.measure(Remote(tags=TAGS))
        self.assertEqual((pin.form, pin.latest, pin.status), ("tag", "v4.1.0", "behind"))

    def test_same_version_takes_its_vxyz_name(self):
        """Of two tags on one release, the pin takes the `vX.Y.Z` name."""
        tags = {"jfalcou/tts": {"4.0": B, "v4.0.0": B}}
        tree = self.tree({"CMakeLists.txt": "CPMAddPackage(NAME TTS GITHUB_REPOSITORY jfalcou/tts GIT_TAG 4.0)\n"})
        [pin] = tree.measure(Remote(tags=tags))
        self.assertEqual((pin.latest, pin.status), ("v4.0.0", "behind"))

    def test_prerelease_and_other_families_are_never_targets(self):
        """A pre-release and a tag of another prefix never become the newest tag of a family."""
        tree = self.tree(
            {
                ".github/workflows/ci.yml": "      - uses: jfalcou/tts/.github/workflows/x.yml@v4.1.0\n"
                "      - uses: jfalcou/tts/.github/workflows/x.yml@hpx-v9.0.0\n"
            }
        )
        found = tree.measure(Remote(tags=TAGS))
        self.assertEqual(
            [(p.current, p.latest, p.status) for p in found],
            [("v4.1.0", "v4.1.0", "current"), ("hpx-v9.0.0", "hpx-v9.0.0", "current")],
        )

    def test_uses_sha_with_tag_comment(self):
        """A `uses:` on a SHA with its tag comment moves both."""
        tree = self.tree({".github/workflows/ci.yml": f"    uses: jfalcou/tts/.github/workflows/x.yml@{A} # v3.0.0\n"})
        self.assertEqual(
            tree.after(Remote(tags=TAGS), ".github/workflows/ci.yml"),
            f"    uses: jfalcou/tts/.github/workflows/x.yml@{C} # v4.1.0\n",
        )

    def test_held_pin_stays(self):
        """A pin under a hold is marked held, and its file is left as it is."""
        tree = self.tree(
            {
                "tools/template/cmake/dependencies.cmake": "CPMAddPackage(NAME TTS"
                " GITHUB_REPOSITORY jfalcou/tts GIT_TAG 4.0)\n"
            }
        )
        holds = [{"source": "jfalcou/tts", "files": ["tools/template/**"], "reason": "test"}]
        [pin] = tree.measure(Remote(tags=TAGS), holds)
        self.assertEqual(pin.status, "held")
        self.assertIsNone(tree.after(Remote(tags=TAGS), "tools/template/cmake/dependencies.cmake", holds))


class Images(Case):
    """The tags of the compilers image."""

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
        found = tree.measure(Remote(images=IMAGES))
        self.assertEqual(
            [(p.current, p.latest, p.status) for p in found],
            [
                ("v10", "v11", "behind"),
                ("mpi-v1", "mpi-v1", "current"),
                ("v9b", "v9b", "fixed"),
                ("eve-2026", "eve-2026", "fixed"),
            ],
        )

    def test_matrix_short_tags(self):
        """The short `image:` of a matrix moves, at the top of the file and in a row, comments excepted."""
        tree = self.tree(
            {
                ".github/matrices/linux.yml": "##   image: v6\nruns-on: ubuntu-latest\nimage: v10\nrows:\n"
                '  - { name: "icpx", preset: "icpx", image: "sycl-v1", setup: "x.sh" }\n'
                '  - { name: "gcc" , preset: "gcc" , image: "v9"    }\n'
            }
        )
        found = tree.measure(Remote(images=IMAGES))
        self.assertEqual(
            [(p.line, p.current, p.latest) for p in found],
            [(3, "v10", "v11"), (5, "sycl-v1", "sycl-v1"), (6, "v9", "v11")],
        )
        self.assertIn('image: "v11"    }', tree.after(Remote(images=IMAGES), ".github/matrices/linux.yml"))

    def test_matrix_of_another_registry_is_not_read(self):
        """A matrix that names its own registry has no pin on the compilers image."""
        tree = self.tree({".github/matrices/cuda.yml": "registry: nvcr.io/nvidia/cuda\nimage: 12.4.0-devel\n"})
        self.assertEqual(tree.measure(Remote(images=IMAGES)), [])

    def test_template_matrices_are_read(self):
        """A matrix below the root, the one of tools/template, is read too."""
        tree = self.tree({"tools/template/.github/matrices/linux.yml": "image: v10\n"})
        self.assertEqual([p.latest for p in tree.measure(Remote(images=IMAGES))], ["v11"])


class LsRemote(unittest.TestCase):
    """The reading of `git ls-remote`."""

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
                (remote.head("x/y"), remote.heads("x/y"), remote.tags("x/y")),
                (A, {"main": A}, {"v1": C, "v2": D}),
            )


if __name__ == "__main__":
    unittest.main()
