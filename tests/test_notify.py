import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from wakeci import main
from wakeci.notify import Message, Notifier, Outbox, Sender, payload, read_url

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
SECRET_URL_PART = "tok" + "en-xyz"  # stands for the secret part of a webhook URL


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.n = Notifier(after=10 * MIN, repeat=timedelta(hours=6))

    def test_a_problem_is_reported_once_it_has_lasted_the_waiting_time(self):
        self.assertIsNone(self.n.problem("source:a", "down", T0))
        self.assertIsNone(self.n.problem("source:a", "down", T0 + 9 * MIN))
        self.assertEqual(self.n.problem("source:a", "down", T0 + 10 * MIN), Message("source:a", "down", problem=True))

    def test_it_is_repeated_every_repeat_period_while_it_lasts(self):
        self.n.problem("k", "x", T0)
        self.assertIsNotNone(self.n.problem("k", "x", T0 + 10 * MIN))
        self.assertIsNone(self.n.problem("k", "x", T0 + 10 * MIN + timedelta(hours=5, minutes=59)))
        self.assertEqual(self.n.problem("k", "still x", T0 + 10 * MIN + timedelta(hours=6)).text, "still x")

    def test_resolved_is_sent_only_for_a_problem_that_was_reported(self):
        self.n.problem("k", "x", T0)
        self.assertIsNone(self.n.clear("k", "ok", T0 + 5 * MIN))  # never reported: silent
        self.n.problem("k", "x", T0 + 6 * MIN)
        self.n.problem("k", "x", T0 + 16 * MIN)
        self.assertEqual(self.n.clear("k", "ok", T0 + 17 * MIN), Message("k", "ok", resolved=True))
        self.assertIsNone(self.n.clear("k", "ok", T0 + 18 * MIN))  # only once

    def test_a_short_outage_restarts_the_wait(self):
        self.n.problem("k", "x", T0)
        self.n.clear("k", "ok", T0 + 5 * MIN)
        self.assertIsNone(self.n.problem("k", "x", T0 + 12 * MIN))  # 0 min into the new outage

    def test_without_waiting_it_is_reported_at_once(self):
        self.assertIsNotNone(self.n.problem("limit", "x", T0, wait=False))
        self.assertIsNone(self.n.problem("limit", "x", T0 + MIN, wait=False))

    def test_keys_are_independent(self):
        self.n.problem("a", "x", T0)
        self.n.problem("b", "y", T0 + 5 * MIN)
        self.assertIsNotNone(self.n.problem("a", "x", T0 + 10 * MIN))
        self.assertIsNone(self.n.problem("b", "y", T0 + 10 * MIN))

    def test_an_event_is_sent_at_most_once_per_repeat_period(self):
        self.assertIsNotNone(self.n.event("wake:host1", "x", T0))
        self.assertIsNone(self.n.event("wake:host1", "x", T0 + timedelta(hours=1)))
        self.assertIsNotNone(self.n.event("wake:host2", "x", T0 + timedelta(hours=1)))
        self.assertIsNotNone(self.n.event("wake:host1", "x", T0 + timedelta(hours=6)))


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.n = Notifier(after=0 * MIN, repeat=timedelta(hours=6))
        self.box = Outbox(delay=5 * MIN, give_up=timedelta(hours=24))
        self.seq = 0

    def send(self, msg):
        self.seq += 1
        self.box.queued(msg, self.seq)
        return self.seq

    def post_and_fail(self, msg, now):
        seq = self.send(msg)
        return self.box.failed(msg, seq, now)

    def keys(self, msgs):
        return [m.key for m in msgs]

    def test_an_undelivered_message_comes_back_after_the_delay_saying_when_it_was_first_tried(self):
        msg = self.n.event("start", "started", T0)
        self.assertTrue(self.post_and_fail(msg, T0))
        self.assertEqual(self.box.due(T0 + 4 * MIN, self.n), [])
        (retry,) = self.box.due(T0 + 5 * MIN, self.n)
        self.assertEqual(retry.text, "started (delayed: first failed 2026-10-22 12:00 UTC)")
        self.assertEqual(self.box.due(T0 + 10 * MIN, self.n), [])  # handed out once per failure

    def test_a_retry_that_fails_again_keeps_the_first_time_and_is_not_counted_again(self):
        msg = self.n.event("start", "started", T0)
        self.post_and_fail(msg, T0)
        (retry,) = self.box.due(T0 + 5 * MIN, self.n)
        self.assertFalse(self.post_and_fail(retry, T0 + 5 * MIN))
        (again,) = self.box.due(T0 + 10 * MIN, self.n)
        self.assertEqual(again.text, "started (delayed: first failed 2026-10-22 12:00 UTC)")

    def test_retries_stop_after_the_give_up_time(self):
        msg = self.n.event("start", "started", T0)
        self.post_and_fail(msg, T0)
        now = T0
        while True:
            now += 5 * MIN
            due = self.box.due(now, self.n)
            if not due:
                break
            self.post_and_fail(due[0], now)
        self.assertGreaterEqual(now - T0, timedelta(hours=24))
        self.assertLess(now - T0, timedelta(hours=24, minutes=10))

    def test_a_resolved_message_is_retried_unless_the_problem_came_back_and_was_reported(self):
        self.n.problem("source:a", "down", T0)
        ok = self.n.clear("source:a", "up", T0 + MIN)
        self.post_and_fail(ok, T0 + MIN)
        (retry,) = self.box.due(T0 + 6 * MIN, self.n)
        self.post_and_fail(retry, T0 + 6 * MIN)
        self.n.problem("source:a", "down again", T0 + 7 * MIN)  # after=0: reported at once
        self.assertEqual(self.box.due(T0 + 11 * MIN, self.n), [])

    def test_a_resolved_message_survives_a_short_unreported_flap(self):
        n = Notifier(after=10 * MIN, repeat=timedelta(hours=6))
        n.problem("source:a", "down", T0)
        n.problem("source:a", "down", T0 + 10 * MIN)  # reported
        ok = n.clear("source:a", "up", T0 + 11 * MIN)
        self.post_and_fail(ok, T0 + 11 * MIN)
        n.problem("source:a", "down", T0 + 15 * MIN)  # back, too short to report
        self.assertEqual(self.box.due(T0 + 16 * MIN, n), [])  # waits
        n.clear("source:a", "up", T0 + 18 * MIN)  # silent: the flap was never reported
        self.assertEqual(self.keys(self.box.due(T0 + 21 * MIN, n)), ["source:a"])

    def test_a_problem_message_is_dropped_when_the_problem_ended(self):
        msg = self.n.problem("source:a", "down", T0)
        self.post_and_fail(msg, T0)
        self.n.clear("source:a", "up", T0 + MIN)
        self.assertEqual(self.box.due(T0 + 5 * MIN, self.n), [])

    def test_a_late_failure_of_an_older_message_is_ignored(self):
        old, new = Message("k", "old"), Message("k", "new")
        seq_old = self.send(old)
        self.send(new)  # sent before the failure of `old` was seen
        self.assertFalse(self.box.failed(old, seq_old, T0))
        self.assertEqual(self.box.due(T0 + 5 * MIN, self.n), [])

    def test_a_newer_message_replaces_a_waiting_retry(self):
        self.post_and_fail(Message("k", "old"), T0)
        self.send(Message("k", "new"))
        self.assertEqual(self.box.due(T0 + 5 * MIN, self.n), [])

    def test_a_delivered_retry_moves_the_repeat_clock(self):
        n = Notifier(after=0 * MIN, repeat=timedelta(hours=6))
        n.problem("limit", "x", T0, wait=False)
        n.resent("limit", T0 + timedelta(hours=5, minutes=59))
        self.assertIsNone(n.problem("limit", "x", T0 + timedelta(hours=6), wait=False))


class PayloadTest(unittest.TestCase):
    def test_formats(self):
        m = Message("source:orgs/o", "text", resolved=True)
        self.assertEqual(payload("discord", m), {"content": "text", "allowed_mentions": {"parse": []}})
        self.assertEqual(payload("slack", m), {"text": "text"})
        self.assertEqual(
            payload("json", m), {"event": "source", "key": "source:orgs/o", "text": "text", "resolved": True}
        )

    def test_discord_text_is_cut_to_its_limit(self):
        self.assertEqual(len(payload("discord", Message("k", "x" * 5000))["content"]), 2000)


class ReadUrlTest(unittest.TestCase):
    def test_only_an_http_url_is_accepted_and_the_content_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "u")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"  https://example.com/hook/{SECRET_URL_PART}\n")
            self.assertEqual(read_url(path), f"https://example.com/hook/{SECRET_URL_PART}")
            with open(path, "w", encoding="utf-8") as f:
                f.write(SECRET_URL_PART)
            with self.assertRaises(ValueError) as e:
                read_url(path)
            self.assertNotIn(SECRET_URL_PART, str(e.exception))

    def test_plain_http_only_to_this_machine(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "u")
            for url, ok in (
                ("http://example.com/hook", False),
                ("http://127.0.0.1:9199/hook", True),
                ("http://localhost/hook", True),
                ("ftp://example.com/x", False),
                ("https://", False),
            ):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(url)
                if ok:
                    self.assertEqual(read_url(path), url)
                else:
                    with self.assertRaises(ValueError, msg=url):
                        read_url(path)


class Receiver:
    """A local webhook that answers with `status` and keeps what it got."""

    def __init__(self, status=204):
        self.bodies, self.headers = [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                outer.headers.append(dict(self.headers))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook/{SECRET_URL_PART}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SenderTest(unittest.TestCase):
    def test_a_message_is_posted_as_json_with_a_user_agent(self):
        r = Receiver()
        self.addCleanup(r.close)
        s = Sender(r.url, "discord", "wakeci/test")
        s.send(Message("start", "hello"))
        s.close()
        self.assertEqual(r.bodies, [{"content": "hello", "allowed_mentions": {"parse": []}}])
        self.assertEqual(r.headers[0]["User-Agent"], "wakeci/test")
        self.assertEqual(s.failed, 0)

    def test_an_error_answer_is_counted_and_the_url_is_never_logged(self):
        r = Receiver(status=500)
        self.addCleanup(r.close)
        s = Sender(r.url, "json", "wakeci/test")
        with self.assertLogs("wakeci", level="INFO") as logs:
            s.send(Message("k", "x"))
            s.close()
        self.assertEqual(s.failed, 1)
        self.assertEqual([m.key for _, m in s.take_undelivered()], ["k"])  # handed back once
        self.assertEqual(s.take_undelivered(), [])
        self.assertNotIn(SECRET_URL_PART, "\n".join(logs.output))

    def test_a_redirect_is_a_failure_not_a_silent_get(self):
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(301)
                self.send_header("Location", "/elsewhere")
                self.end_headers()

            def do_GET(self):  # a followed redirect would land here and look like success
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        s = Sender(f"http://127.0.0.1:{server.server_port}/hook", "discord", "ua")
        s.send(Message("k", "x"))
        s.close()
        self.assertEqual((s.failed, [m.key for _, m in s.take_undelivered()]), (1, ["k"]))

    def test_an_unreachable_webhook_is_counted_and_the_url_is_never_logged(self):
        r = Receiver()
        url = r.url
        r.close()  # nothing listens on that port any more
        s = Sender(url, "json", "wakeci/test", timeout=2)
        with self.assertLogs("wakeci", level="INFO") as logs:
            s.send(Message("k", "x"))
            s.close()
        self.assertEqual(s.failed, 1)
        self.assertNotIn(SECRET_URL_PART, "\n".join(logs.output))

    def test_a_full_queue_drops_and_counts_instead_of_blocking(self):
        gate = threading.Event()

        def slow(req, timeout):
            gate.wait(5)
            raise OSError("down")

        s = Sender("https://example.com/x", "json", "ua", opener=slow, size=1)
        for i in range(5):
            s.send(Message(f"k{i}", "x"))  # must return at once
        gate.set()
        s.close()
        self.assertGreaterEqual(s.failed, 3)

    def test_an_unknown_format_is_refused(self):
        with self.assertRaises(ValueError):
            Sender("https://example.com/x", "teams", "ua")


class WiringTest(unittest.TestCase):
    def test_without_a_notify_block_nothing_is_sent(self):
        notifier, sender = main.notifications({}, "v")
        self.assertIsNone(sender)
        self.assertIsInstance(notifier, Notifier)

    def test_a_notify_block_builds_a_sender_with_its_timings(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "u")
            with open(path, "w", encoding="utf-8") as f:
                f.write("https://example.com/hook\n")
            notifier, sender = main.notifications(
                {"notify": {"url_file": path, "format": "slack", "after_minutes": 3, "repeat_hours": 2}}, "v"
            )
            self.addCleanup(sender.close)
        self.assertEqual((notifier.after, notifier.repeat), (3 * MIN, timedelta(hours=2)))


if __name__ == "__main__":
    unittest.main()
