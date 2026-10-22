import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
# Built at run time: this file is scanned by the guard too.
PRIV = ".".join(["192", "168", "1", "10"])


class PrePushHookTest(unittest.TestCase):
    """scripts/install-hooks.sh and scripts/pre-push against a real `git push`."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_COMMITTER_NAME="t",
                        GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_EMAIL="t@example.com")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.work = os.path.join(self.tmp, "work")
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)
        subprocess.run(["git", "init", "-q", self.work], check=True)
        self.git("remote", "add", "origin", self.remote)
        os.makedirs(os.path.join(self.work, "scripts"))
        for name in ("__init__.py", "public_safe.py", "pre-push", "install-hooks.sh"):
            shutil.copy2(HERE / "scripts" / name, os.path.join(self.work, "scripts", name))
        self.commit("start", {"a.txt": "broadcast 192.0.2.255\n"})
        out = self.run_cmd(["bash", "scripts/install-hooks.sh"])
        self.assertEqual(out.returncode, 0, out.stderr)

    def run_cmd(self, cmd):
        return subprocess.run(cmd, cwd=self.work, capture_output=True, text=True, env=self.env)

    def git(self, *args):
        out = self.run_cmd(["git", "-c", "commit.gpgSign=false", *args])
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def commit(self, message, files):
        for name, text in files.items():
            with open(os.path.join(self.work, name), "w", encoding="utf-8") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def push(self, *refs):
        return self.run_cmd(["git", "push", "-q", "origin", *refs])

    def remote_sha(self, ref):
        out = subprocess.run(["git", "--git-dir", self.remote, "rev-parse", "--verify", "-q", ref],
                             capture_output=True, text=True)
        return out.stdout.strip() or None

    def test_clean_push_goes_through(self):
        branch = self.git("branch", "--show-current")
        self.assertEqual(self.push(branch).returncode, 0)
        self.assertEqual(self.remote_sha(branch), self.git("rev-parse", "HEAD"))

    def test_leak_anywhere_in_the_pushed_history_is_refused(self):
        branch = self.git("branch", "--show-current")
        self.commit("add host", {"b.txt": f"host {PRIV}\n"})
        self.commit("remove host again", {"b.txt": "host 192.0.2.10\n"})
        out = self.push(branch)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(f"private address {PRIV}", out.stderr)
        self.assertIn("push refused", out.stderr)
        self.assertIsNone(self.remote_sha(branch))

    def test_a_branch_other_than_head_is_checked(self):
        main = self.git("branch", "--show-current")
        self.git("switch", "-q", "-c", "leak")
        self.commit("add host", {"b.txt": f"host {PRIV}\n"})
        self.git("switch", "-q", main)
        out = self.push("leak")
        self.assertNotEqual(out.returncode, 0)
        self.assertIsNone(self.remote_sha("leak"))

    def test_deleting_a_remote_branch_is_allowed(self):
        branch = self.git("branch", "--show-current")
        self.assertEqual(self.push(branch).returncode, 0)
        self.git("switch", "-q", "-c", "other")
        self.assertEqual(self.push("other").returncode, 0)
        self.assertEqual(self.push("--delete", "other").returncode, 0)
        self.assertIsNone(self.remote_sha("other"))

    def test_installer_does_not_replace_a_different_hook_without_force(self):
        hook = Path(self.work, ".git", "hooks", "pre-push")
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.assertNotEqual(self.run_cmd(["bash", "scripts/install-hooks.sh"]).returncode, 0)
        self.assertEqual(self.run_cmd(["bash", "scripts/install-hooks.sh", "--force"]).returncode, 0)
        self.assertEqual(hook.read_bytes(), (HERE / "scripts" / "pre-push").read_bytes())


if __name__ == "__main__":
    unittest.main()
