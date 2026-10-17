import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from rackctl import actions, github, main
from rackctl.decide import Machine

M = Machine("host1", "00:00:5e:00:53:01", "192.0.2.128", "host1", ("host1-a-",), 1)
HERE = Path(__file__).resolve().parents[1]


class MagicPacketTest(unittest.TestCase):
    def test_six_ff_then_mac_sixteen_times(self):
        p = actions.magic_packet("00:00:5e:00:53:01")
        self.assertEqual(len(p), 102)
        self.assertEqual(p[:6], b"\xff" * 6)
        self.assertEqual(p[6:12], bytes.fromhex("00005e005301"))
        self.assertEqual(p[6:], bytes.fromhex("00005e005301") * 16)

    def test_bad_mac_is_refused(self):
        for bad in ("", "00:00:5e", "zz:00:5e:00:53:01", "00:00:5e:00:53:01:00"):
            with self.assertRaises(ValueError):
                actions.magic_packet(bad)


class SleepTest(unittest.TestCase):
    def fake(self, code=0, timeout=False):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if timeout:
                raise subprocess.TimeoutExpired(cmd, 30)
            return subprocess.CompletedProcess(cmd, code, "", "Permission denied")

        return calls, run

    def test_command_sends_no_remote_command(self):
        cmd = actions.ssh_sleep_command(M, "/k", "/kh")
        self.assertEqual(cmd[-1], "host1@192.0.2.128")
        self.assertIn("BatchMode=yes", cmd)
        self.assertIn("StrictHostKeyChecking=yes", cmd)
        self.assertIn("UserKnownHostsFile=/kh", cmd)

    def test_exit_codes(self):
        for code, word in ((0, "užmigs"), (3, "job'as"), (4, "persikraus"), (255, "SSH nepavyko"), (9, "nežinomas")):
            _, run = self.fake(code)
            self.assertIn(word, actions.put_to_sleep(M, "/k", "/kh", run=run))

    def test_timeout(self):
        _, run = self.fake(timeout=True)
        self.assertIn("neatsakė", actions.put_to_sleep(M, "/k", "/kh", run=run))


class FakeResponse(io.BytesIO):
    def __init__(self, body, etag=None):
        super().__init__(json.dumps(body).encode())
        self.headers = {"ETag": etag} if etag else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    def __init__(self, routes):
        self.routes = routes
        self.seen = []

    def __call__(self, req, timeout):
        url = req.full_url.replace(github.API + "/", "")
        self.seen.append((url, req.headers.get("If-none-match")))
        for prefix, (body, etag) in self.routes.items():
            if url.startswith(prefix):
                if etag and req.headers.get("If-none-match") == etag:
                    raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
                return FakeResponse(body, etag)
        return FakeResponse({})


ROUTES = {
    "repos/o/r/actions/runners": (
        {"runners": [
            {"name": "host1-a-1", "status": "online", "busy": True, "labels": [{"name": "self-hosted"}, {"name": "light"}]},
            {"name": "host2-a-1", "status": "offline", "busy": False, "labels": [{"name": "npm"}]},
        ]},
        '"r1"',
    ),
    "repos/o/r/actions/runs?status=queued": ({"workflow_runs": [{"id": 7}]}, None),
    "repos/o/r/actions/runs?status=in_progress": ({"workflow_runs": []}, None),
    "repos/o/r/actions/runs/7/jobs": (
        {"jobs": [
            {"status": "queued", "labels": ["self-hosted", "home", "npm"]},
            {"status": "queued", "labels": ["ubuntu-24.04"]},
            {"status": "in_progress", "labels": ["self-hosted", "home", "light"]},
        ]},
        None,
    ),
}


class GitHubTest(unittest.TestCase):
    def test_observe(self):
        gh = github.GitHub([github.Source("repos/o/r", ("o/r",), "t")], opener=FakeGitHub(ROUTES))
        obs = gh.observe()
        self.assertEqual([(r.name, r.online, r.busy) for r in obs.runners], [("host1-a-1", True, True), ("host2-a-1", False, False)])
        # Tik eilėje laukiantys savo runner'io job'ai; GitHub runner'io job'as ne.
        self.assertEqual(obs.queued, (frozenset({"self-hosted", "home", "npm"}),))

    def test_etag_is_sent_and_304_reuses_the_body(self):
        fake = FakeGitHub(ROUTES)
        gh = github.GitHub([github.Source("repos/o/r", ("o/r",), "t")], opener=fake)
        gh.observe()
        again = gh.observe()
        sent = [etag for url, etag in fake.seen if url.startswith("repos/o/r/actions/runners")]
        self.assertEqual(sent, [None, '"r1"'])
        self.assertEqual(len(again.runners), 2)

    def test_cache_keeps_only_what_the_last_poll_used(self):
        fake = FakeGitHub(dict(ROUTES))
        gh = github.GitHub([github.Source("repos/o/r", ("o/r",), "t")], opener=fake)
        gh.observe()
        self.assertTrue(any("runners" in k for k in gh._cache))
        gh.sources = []
        gh.observe()
        self.assertEqual(gh._cache, {})


class ConfigTest(unittest.TestCase):
    def test_example_config_loads_and_defaults_to_dry_run(self):
        cfg = main.load_config(str(HERE / "orangepi" / "config.example.json"))
        self.assertTrue(cfg["dry_run"])
        machines = main.machines_from(cfg)
        self.assertEqual([m.name for m in machines], ["host1", "host2", "host3", "host4"])
        for m in machines:
            actions.magic_packet(m.mac)

    def test_last_online_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "sub" / "state.json")
            when = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
            main.save_last_online(path, {"host1": when})
            self.assertEqual(main.load_last_online(path), {"host1": when})

    def test_missing_or_broken_state_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main.load_last_online(str(Path(d) / "none.json")), {})
            broken = Path(d) / "b.json"
            broken.write_text("{")
            self.assertEqual(main.load_last_online(str(broken)), {})


if __name__ == "__main__":
    unittest.main()
