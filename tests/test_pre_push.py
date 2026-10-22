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
        # Hermetic: none of the user's git configuration (a global core.hooksPath would put
        # the hook into the user's real hooks directory).
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_COMMITTER_NAME="t",
                        GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_EMAIL="t@example.com",
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", HOME=self.tmp)
        self.remote = os.path.join(self.tmp, "remote.git")
        self.work = os.path.join(self.tmp, "work")
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True, env=self.env)
        subprocess.run(["git", "init", "-q", self.work], check=True, env=self.env)
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
                             capture_output=True, text=True, env=self.env)
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

    def test_annotated_tag_with_a_leak_in_its_message_is_refused(self):
        self.git("tag", "-a", "v1", "-m", f"release, host {PRIV}")
        out = self.push("v1")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(f"private address {PRIV}", out.stderr)
        self.assertIsNone(self.remote_sha("refs/tags/v1"))

    def test_tag_object_pushed_by_id_without_a_local_tag_is_refused(self):
        self.git("tag", "-a", "v2", "-m", f"host {PRIV}")
        sha = self.git("rev-parse", "v2")
        self.git("tag", "-d", "v2")
        out = self.push(f"{sha}:refs/tags/v2")
        self.assertNotEqual(out.returncode, 0)
        self.assertIsNone(self.remote_sha("refs/tags/v2"))

    def test_leak_in_a_nested_tag_is_refused(self):
        self.git("tag", "-a", "inner", "-m", f"host {PRIV}")
        self.git("tag", "-a", "outer", "-m", "fine", "inner")
        self.git("tag", "-d", "inner")
        out = self.push("outer")
        self.assertNotEqual(out.returncode, 0)
        self.assertIsNone(self.remote_sha("refs/tags/outer"))

    def test_a_local_tag_that_is_not_pushed_does_not_block_a_clean_push(self):
        branch = self.git("branch", "--show-current")
        self.git("tag", "-a", "private-note", "-m", f"host {PRIV}")
        out = self.push(branch)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_an_old_guard_without_rev_refuses_with_a_clear_message(self):
        guard = Path(self.work, "scripts", "public_safe.py")
        guard.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
        out = self.push(self.git("branch", "--show-current"))
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("could not check", out.stderr)

    def test_installer_refuses_when_core_hooks_path_is_set(self):
        self.git("config", "core.hooksPath", ".githooks")
        out = self.run_cmd(["bash", "scripts/install-hooks.sh", "--force"])
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("core.hooksPath", out.stderr)
        self.assertFalse(Path(self.work, ".githooks", "pre-push").exists())

    def test_installer_does_not_replace_a_different_hook_without_force(self):
        hook = Path(self.work, ".git", "hooks", "pre-push")
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.assertNotEqual(self.run_cmd(["bash", "scripts/install-hooks.sh"]).returncode, 0)
        self.assertEqual(self.run_cmd(["bash", "scripts/install-hooks.sh", "--force"]).returncode, 0)
        self.assertEqual(hook.read_bytes(), (HERE / "scripts" / "pre-push").read_bytes())


if __name__ == "__main__":
    unittest.main()
