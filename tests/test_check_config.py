import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "orangepi"))

import check_config  # noqa: E402

EXAMPLE = json.loads((HERE / "orangepi" / "config.example.json").read_text(encoding="utf-8"))


def run(cfg):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "c.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f) if not isinstance(cfg, str) else f.write(cfg)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_config.run([path])
    return code, out.getvalue(), err.getvalue()


def changed(**edits):
    cfg = json.loads(json.dumps(EXAMPLE))
    for key, fn in edits.items():
        fn(cfg) if callable(fn) else cfg.__setitem__(key, fn)
    return cfg


class CheckConfigTest(unittest.TestCase):
    def test_example_passes_and_lists_every_path(self):
        code, out, _ = run(EXAMPLE)
        self.assertEqual(code, 0)
        lines = [l.split("\t") for l in out.splitlines()]
        self.assertEqual([k for k, _ in lines].count("token"), 2)
        self.assertEqual([k for k, _ in lines].count("host"), 3)
        self.assertIn(["ssh_key", "/etc/rackctl/id_ed25519"], lines)

    def test_bad_configurations_are_refused(self):
        cases = {
            "not json": "{",
            "no ssh_key": changed(ssh_key=lambda c: c.pop("ssh_key")),
            "relative key": changed(ssh_key="id_ed25519"),
            "no sources": changed(sources=[]),
            "bad runners_path": changed(sources=lambda c: c["sources"][0].update(runners_path="example-org")),
            "relative token": changed(sources=lambda c: c["sources"][0].update(token_file="token")),
            "no machines": changed(machines=[]),
            "duplicate name": changed(machines=lambda c: c["machines"][1].update(name="host1")),
            "bad mac": changed(machines=lambda c: c["machines"][0].update(mac="00:00:5e")),
            "host with a space": changed(machines=lambda c: c["machines"][0].update(host="a b")),
            "host with a shell char": changed(machines=lambda c: c["machines"][0].update(host="a;b")),
            "bad ssh_user": changed(machines=lambda c: c["machines"][0].update(ssh_user="Root$")),
            "no prefixes": changed(machines=lambda c: c["machines"][0].update(runner_prefixes=[])),
        }
        for name, cfg in cases.items():
            code, out, err = run(cfg)
            self.assertEqual((code, out), (1, ""), name)
            self.assertIn("invalid configuration", err, name)


if __name__ == "__main__":
    unittest.main()
