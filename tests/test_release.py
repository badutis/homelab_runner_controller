import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import release

CHANGELOG = """# Changelog

## [Unreleased]

## [0.4.0] - 2026-10-22

### Changed
- One name everywhere.

## [0.3.0] - 2026-10-22

- Notifications.
"""


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(
            os.environ,
            GIT_AUTHOR_NAME="t",
            GIT_COMMITTER_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_EMAIL="t@example.com",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            HOME=self.tmp.name,
        )
        self.git("init", "-q")
        self.write("0.4.0", CHANGELOG)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "release")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, env=self.env)

    def write(self, version, changelog):
        (self.root / "wakeci").mkdir(exist_ok=True)
        (self.root / "wakeci" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        if changelog is None:
            (self.root / "CHANGELOG.md").unlink(missing_ok=True)
        else:
            (self.root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")

    def refused(self, tag=None):
        old = os.environ.copy()
        os.environ.update(self.env)
        try:
            with self.assertRaises(SystemExit) as e:
                release.check(tag, self.root)
        finally:
            os.environ.clear()
            os.environ.update(old)
        return str(e.exception)

    def passes(self, tag=None):
        old = os.environ.copy()
        os.environ.update(self.env)
        try:
            return release.check(tag, self.root)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_notes_are_the_versions_section_only(self):
        self.assertEqual(release.notes("0.4.0", self.root), "### Changed\n- One name everywhere.")
        self.assertEqual(release.notes("0.3.0", self.root), "- Notifications.")

    def test_without_a_tag_the_version_and_changelog_are_checked(self):
        self.assertEqual(self.passes(), "0.4.0")
        self.write("0.5.0.dev0", CHANGELOG)
        self.assertIn("not a release version", self.refused())
        self.write("0.5.0", CHANGELOG)
        self.assertIn("no section for 0.5.0", self.refused())
        self.write("0.4.0", None)
        self.assertIn("CHANGELOG.md is missing", self.refused())
        self.write("0.4.0", "# Changelog\n\n## [0.4.0] - 2026-10-22\n\n## [0.3.0]\n- x\n")
        self.assertIn("no section for 0.4.0", self.refused())  # an empty section is no section

    def test_an_annotated_tag_on_main_with_the_right_name_passes(self):
        self.git("tag", "-a", "v0.4.0", "-m", "v0.4.0")
        self.assertEqual(self.passes("v0.4.0"), "0.4.0")

    def test_a_tag_with_another_version_is_refused(self):
        self.git("tag", "-a", "v0.4.1", "-m", "x")
        self.assertIn("does not match", self.refused("v0.4.1"))

    def test_a_lightweight_tag_is_refused(self):
        self.git("tag", "v0.4.0")
        self.assertIn("not an annotated tag", self.refused("v0.4.0"))

    def test_a_tag_off_main_is_refused(self):
        self.git("switch", "-q", "-c", "side")
        (self.root / "x").write_text("x", encoding="utf-8")
        self.git("add", "x")
        self.git("commit", "-q", "-m", "side")
        self.git("tag", "-a", "v0.4.0", "-m", "v0.4.0")
        self.assertIn("not on origin/main", self.refused("v0.4.0"))


if __name__ == "__main__":
    unittest.main()
