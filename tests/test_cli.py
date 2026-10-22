import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wakeci import __version__, cli, main

EXAMPLE = Path(__file__).resolve().parent.parent / "controller" / "config.example.json"


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(args))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def write(d, cfg):
    path = os.path.join(d, "c.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


class CliTest(unittest.TestCase):
    def test_version(self):
        self.assertEqual(run("version"), (0, f"wakeci {__version__}\n", ""))

    def test_check_of_the_example_is_clean(self):
        code, out, err = run("check", str(EXAMPLE))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("valid (3 machines)", out)

    def test_check_paths_prints_what_the_installer_prepares(self):
        code, out, _ = run("check", "--paths", str(EXAMPLE))
        self.assertEqual(code, 0)
        self.assertIn("ssh_key\t/etc/wakeci/id_ed25519", out.splitlines())

    def test_an_invalid_file_exits_one_with_the_reason(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            cfg["idle_minute"] = 30  # typo
            code, out, err = run("check", write(d, cfg))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("unknown keys: idle_minute", err)

    def test_a_missing_config_version_is_a_warning_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            del cfg["config_version"]
            code, _, err = run("check", write(d, cfg))
        self.assertEqual(code, 0)
        self.assertIn("warning: no config_version", err)

    def test_an_unsupported_config_version_is_refused_by_check_and_run(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            cfg["config_version"] = 2
            path = write(d, cfg)
            code, _, err = run("check", path)
            self.assertEqual(code, 1)
            self.assertIn("config_version 2 is not supported", err)
            with self.assertRaises(ValueError):
                main.load_config(path)

    def test_run_and_the_old_form_start_the_controller(self):
        for args in (("run", str(EXAMPLE)), (str(EXAMPLE),)):
            with mock.patch.object(main, "run") as started:
                code, _, _ = run(*args)
            self.assertEqual(code, 0, args)
            self.assertEqual(started.call_args[0][0]["config_version"], 1, args)

    def test_the_old_form_with_a_missing_file_says_not_found(self):
        with self.assertRaises(FileNotFoundError):
            cli.main(["/nonexistent/wakeci.json"])

    def test_run_warns_in_the_log_when_config_version_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            del cfg["config_version"]
            path = write(d, cfg)
            with mock.patch.object(main, "run"), self.assertLogs("wakeci", level="WARNING") as logs:
                cli.main(["run", path])
        self.assertIn("no config_version", "\n".join(logs.output))

    def test_no_command_is_a_usage_error(self):
        code, _, err = run()
        self.assertEqual(code, 2)
        self.assertIn("COMMAND", err)


if __name__ == "__main__":
    unittest.main()
