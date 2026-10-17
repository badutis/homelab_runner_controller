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
        self.assertEqual([q.labels for q in obs.queued], [frozenset({"self-hosted", "home", "npm"})])
        self.assertEqual({q.source for q in obs.queued} | {r.source for r in obs.runners}, {"repos/o/r"})
        self.assertTrue(obs.complete)

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

    def test_state_round_trip_keeps_both_fields(self):
        from rackctl.decide import MachineState

        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "sub" / "state.json")
            when = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
            s = MachineState(last_online=when, hold_until=when)
            main.save_state(path, main.snapshot({"host1": s, "host2": MachineState()}))
            self.assertEqual(main.load_state(path), {"host1": {"last_online": when, "hold_until": when}})

    def test_bad_state_files_are_ignored_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main.load_state(str(Path(d) / "none.json")), {})
            for text in ("{", "[]", '{"host1": "2026-10-17"}', '{"host1": {"last_online": "vakar"}}'):
                f = Path(d) / "b.json"
                f.write_text(text)
                self.assertEqual(main.load_state(str(f)), {} if text in ("{", "[]", '{"host1": "2026-10-17"}') else {"host1": {}})

    def test_naive_time_in_state_is_read_as_utc(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.json"
            f.write_text('{"host1": {"last_online": "2026-10-17T12:00:00"}}')
            got = main.load_state(str(f))["host1"]["last_online"]
            self.assertEqual(got, datetime(2026, 10, 17, 12, tzinfo=timezone.utc))


class GitHubResilienceTest(unittest.TestCase):
    def test_one_broken_source_does_not_blind_the_other(self):
        class Broken(FakeGitHub):
            def __call__(self, req, timeout):
                if "orgs/bad" in req.full_url:
                    raise urllib.error.HTTPError(req.full_url, 401, "Bad credentials", {}, None)
                return super().__call__(req, timeout)

        gh = github.GitHub(
            [github.Source("repos/o/r", ("o/r",), "t"), github.Source("orgs/bad", ("bad/x",), "t2")],
            opener=Broken(ROUTES),
        )
        obs = gh.observe()
        self.assertFalse(obs.complete)
        self.assertEqual(len(obs.runners), 2)
        self.assertEqual(len(obs.queued), 1)
        self.assertIn("401", gh.errors["orgs/bad"])

    def test_in_progress_jobs_are_not_refetched_every_poll(self):
        routes = dict(ROUTES)
        routes["repos/o/r/actions/runs?status=queued"] = ({"workflow_runs": []}, None)
        routes["repos/o/r/actions/runs?status=in_progress"] = ({"workflow_runs": [{"id": 7, "updated_at": "t1"}]}, None)
        fake = FakeGitHub(routes)
        now = [datetime(2026, 10, 17, 12, tzinfo=timezone.utc)]
        gh = github.GitHub([github.Source("repos/o/r", ("o/r",), "t")], opener=fake, clock=lambda: now[0])

        def job_calls():
            return sum(1 for url, _ in fake.seen if "/runs/7/jobs" in url)

        gh.observe()
        gh.observe()
        self.assertEqual(job_calls(), 1)
        self.assertEqual(len(gh.observe().queued), 1)  # atsakymas imamas iš atminties
        now[0] = now[0] + github.IN_PROGRESS_REFRESH
        gh.observe()
        self.assertEqual(job_calls(), 2)

    def test_job_taken_after_the_run_started_is_not_reported_from_cache(self):
        routes = dict(ROUTES)
        routes["repos/o/r/actions/runs?status=queued"] = ({"workflow_runs": [{"id": 7, "updated_at": "t1"}]}, None)
        routes["repos/o/r/actions/runs?status=in_progress"] = ({"workflow_runs": []}, None)
        fake = FakeGitHub(routes)
        gh = github.GitHub([github.Source("repos/o/r", ("o/r",), "t")], opener=fake)
        self.assertEqual(len(gh.observe().queued), 1)
        # Runner'is paėmė job'ą: paleidimas dabar in_progress, job'ų sąrašas pasikeitė.
        fake.routes["repos/o/r/actions/runs?status=queued"] = ({"workflow_runs": []}, None)
        fake.routes["repos/o/r/actions/runs?status=in_progress"] = ({"workflow_runs": [{"id": 7, "updated_at": "t2"}]}, None)
        fake.routes["repos/o/r/actions/runs/7/jobs"] = ({"jobs": [{"status": "in_progress", "labels": ["self-hosted", "home", "npm"]}]}, None)
        self.assertEqual(gh.observe().queued, ())

    def test_changed_in_progress_run_is_refetched(self):
        routes = dict(ROUTES)
        routes["repos/o/r/actions/runs?status=queued"] = ({"workflow_runs": []}, None)
        routes["repos/o/r/actions/runs?status=in_progress"] = ({"workflow_runs": [{"id": 7, "updated_at": "t1"}]}, None)
        fake = FakeGitHub(routes)
        gh = github.GitHub([github.Source("repos/o/r", ("o/r",), "t")], opener=fake)
        self.assertEqual(len(gh.observe().queued), 1)
        fake.routes["repos/o/r/actions/runs?status=in_progress"] = ({"workflow_runs": [{"id": 7, "updated_at": "t2"}]}, None)
        fake.routes["repos/o/r/actions/runs/7/jobs"] = ({"jobs": []}, None)
        self.assertEqual(gh.observe().queued, ())

    def test_low_rate_limit_asks_to_wait_until_reset(self):
        class Limited(FakeGitHub):
            def __call__(self, req, timeout):
                resp = super().__call__(req, timeout)
                resp.headers = dict(resp.headers, **{"X-RateLimit-Remaining": "42", "X-RateLimit-Reset": "1792000000"})
                return resp

        gh = github.GitHub([github.Source("repos/o/r", ("o/r",), "t")], opener=Limited(ROUTES))
        gh.observe()
        self.assertEqual(gh.remaining["repos/o/r"], 42)
        self.assertEqual(gh.backoff_until(), datetime.fromtimestamp(1792000000, timezone.utc))

    def test_created_at_is_parsed(self):
        self.assertEqual(github.parse_time("2026-10-17T10:15:34Z"), datetime(2026, 10, 17, 10, 15, 34, tzinfo=timezone.utc))
        self.assertIsNone(github.parse_time("vakar"))


if __name__ == "__main__":
    unittest.main()
