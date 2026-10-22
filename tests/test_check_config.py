import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "controller"))

import check_config  # noqa: E402

EXAMPLE = json.loads((HERE / "controller" / "config.example.json").read_text(encoding="utf-8"))


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
        self.assertEqual([k for k, _ in lines].count("token"), 3)  # two sources and the webhook URL
        self.assertEqual([k for k, _ in lines].count("host"), 3)
        self.assertIn(["ssh_key", "/etc/wakeci/id_ed25519"], lines)

    def test_notify_is_optional_and_its_url_file_is_listed_as_a_token(self):
        self.assertEqual(run(changed(notify=lambda c: c.pop("notify")))[0], 0)
        code, out, _ = run(EXAMPLE)
        self.assertIn("token\t/etc/wakeci/notify-url", out.splitlines())

    def test_sources_may_share_one_token_file(self):
        cfg = changed(sources=lambda c: c["sources"][1].update(token_file=c["sources"][0]["token_file"]))
        self.assertEqual(run(cfg)[0], 0)

    def test_a_mac_without_separators_is_accepted(self):
        self.assertEqual(run(changed(machines=lambda c: c["machines"][0].update(mac="00005e005301")))[0], 0)

    def test_bad_configurations_are_refused(self):
        cases = {
            "not json": "{",
            "no ssh_key": changed(ssh_key=lambda c: c.pop("ssh_key")),
            "relative key": changed(ssh_key="id_ed25519"),
            "key outside /etc/wakeci": changed(ssh_key="/etc/id_wakeci"),
            "known_hosts outside /etc/wakeci": changed(known_hosts="/etc/ssh/ssh_known_hosts"),
            "key in a subdirectory": changed(ssh_key="/etc/wakeci/keys/id"),
            "key is the config": changed(ssh_key="/etc/wakeci/config.json"),
            "known_hosts is the public key": changed(known_hosts="/etc/wakeci/id_ed25519.pub"),
            "token is known_hosts": changed(sources=lambda c: c["sources"][0].update(token_file="/etc/wakeci/known_hosts")),
            "token is the config": changed(sources=lambda c: c["sources"][0].update(token_file="/etc/wakeci/config.json")),
            "token outside /etc/wakeci": changed(sources=lambda c: c["sources"][0].update(token_file="/var/lib/wakeci/t")),
            "dry_run as text": changed(dry_run="false"),
            "idle_minutes as text": changed(idle_minutes="30"),
            "poll_seconds zero": changed(poll_seconds=0),
            "metrics_listen without address": changed(metrics_listen="9105"),
            "broadcast not an address": changed(broadcast="lan"),
            "host starting with a dash": changed(machines=lambda c: c["machines"][0].update(host="-oProxyCommand=x")),
            "host with an IPv6 scope": changed(machines=lambda c: c["machines"][0].update(host="fe" + "80::1%$(reboot)")),
            "prefix not a string": changed(machines=lambda c: c["machines"][0].update(runner_prefixes=[1])),
            "unknown top-level key": changed(idle_minute=30),
            "config_version true": changed(config_version=True),
            "config_version 1.0": changed(config_version=1.0),
            "rank a fraction": changed(machines=lambda c: c["machines"][0].update(rank=1.7)),
            "rank as text": changed(machines=lambda c: c["machines"][0].update(rank="1")),
            "path with a newline": changed(ssh_key="/etc/wakeci/id_ed25519\n"),
            "ssh_user with a newline": changed(machines=lambda c: c["machines"][0].update(ssh_user="wakeci\n")),
            "repo with a newline": changed(sources=lambda c: c["sources"][0].update(repos=["o/r\n"])),
            "unknown source key": changed(sources=lambda c: c["sources"][0].update(token="x")),
            "unknown machine key": changed(machines=lambda c: c["machines"][0].update(ip="x")),
            "machines not a list": changed(machines={"a": 1}),
            "notify not an object": changed(notify="yes"),
            "notify unknown key": changed(notify=lambda c: c["notify"].update(channel="x")),
            "notify url_file outside /etc/wakeci": changed(notify=lambda c: c["notify"].update(url_file="/tmp/u")),
            "notify url_file is a token": changed(notify=lambda c: c["notify"].update(url_file=c["sources"][0]["token_file"])),
            "notify url_file is the key": changed(notify=lambda c: c["notify"].update(url_file="/etc/wakeci/id_ed25519")),
            "notify bad format": changed(notify=lambda c: c["notify"].update(format="teams")),
            "notify no format": changed(notify=lambda c: c["notify"].pop("format")),
            "notify negative wait": changed(notify=lambda c: c["notify"].update(after_minutes=-1)),
            "notify zero repeat": changed(notify=lambda c: c["notify"].update(repeat_hours=0)),
            "notify repeat as text": changed(notify=lambda c: c["notify"].update(repeat_hours="6")),
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
