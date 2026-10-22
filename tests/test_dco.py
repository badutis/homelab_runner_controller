import os
import subprocess
import tempfile
import unittest

from scripts import dco


class DcoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Ann",
            GIT_AUTHOR_EMAIL="ann@example.com",
            GIT_COMMITTER_NAME="Ann",
            GIT_COMMITTER_EMAIL="ann@example.com",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            HOME=self.tmp.name,
        )
        self.git("init", "-q")
        self.base = self.commit("base", sign=True)
        self.old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.old)

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.tmp.name, check=True, capture_output=True, text=True, env=self.env
        ).stdout.strip()

    def commit(self, msg, sign, author=None):
        args = ["commit", "-q", "--allow-empty", "-m", msg]
        if sign:
            args.append("-s")
        if author:
            args += ["--author", author]
        self.git(*args)
        return self.git("rev-parse", "HEAD")

    def test_signed_commits_pass(self):
        head = self.commit("feat: x", sign=True)
        self.assertEqual(dco.unsigned(self.base, head), [])

    def test_an_unsigned_commit_is_named(self):
        bad = self.commit("feat: x", sign=False)
        head = self.commit("fix: y", sign=True)
        self.assertEqual([m.split()[0] for m in dco.unsigned(self.base, head)], [bad[:12]])

    def test_a_sign_off_by_someone_else_does_not_count(self):
        head = self.commit("feat: x\n\nSigned-off-by: Bob <bob@example.com>", sign=False)
        self.assertEqual(len(dco.unsigned(self.base, head)), 1)

    def test_the_author_email_must_match_not_the_committer(self):
        head = self.commit(
            "feat: x\n\nSigned-off-by: Carl <carl@example.com>", sign=False, author="Carl <carl@example.com>"
        )
        self.assertEqual(dco.unsigned(self.base, head), [])

    def test_dependabot_commits_are_exempt(self):
        bot = "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>"
        head = self.commit("build: bump", sign=False, author=bot)
        self.assertEqual(dco.unsigned(self.base, head), [])

    def test_bad_arguments_are_a_usage_error(self):
        self.assertEqual(dco.main(["HEAD", "main"]), 2)


if __name__ == "__main__":
    unittest.main()
