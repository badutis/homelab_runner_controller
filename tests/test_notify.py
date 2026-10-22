import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from rackctl import main
from rackctl.notify import Message, Notifier, Sender, payload, read_url

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
SECRET_URL_PART = "tok" + "en-xyz"  # stands for the secret part of a webhook URL


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.n = Notifier(after=10 * MIN, repeat=timedelta(hours=6))

    def test_a_problem_is_reported_once_it_has_lasted_the_waiting_time(self):
        self.assertIsNone(self.n.problem("source:a", "down", T0))
        self.assertIsNone(self.n.problem("source:a", "down", T0 + 9 * MIN))
        self.assertEqual(self.n.problem("source:a", "down", T0 + 10 * MIN), Message("source:a", "down"))

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


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.n = Notifier(after=10 * MIN, repeat=timedelta(hours=6))

    def test_an_undelivered_problem_is_tried_again_after_five_minutes(self):
        self.n.problem("k", "x", T0)
        self.assertIsNotNone(self.n.problem("k", "x", T0 + 10 * MIN))
        self.n.not_delivered("k", T0 + 10 * MIN)
        self.assertIsNone(self.n.problem("k", "x", T0 + 14 * MIN))
        self.assertIsNotNone(self.n.problem("k", "x", T0 + 15 * MIN))

    def test_an_undelivered_event_is_tried_again_after_five_minutes(self):
        self.n.event("wake:host1", "x", T0)
        self.n.not_delivered("wake:host1", T0)
        self.assertIsNone(self.n.event("wake:host1", "x", T0 + 4 * MIN))
        self.assertIsNotNone(self.n.event("wake:host1", "x", T0 + 5 * MIN))

    def test_a_problem_that_ended_meanwhile_is_not_retried(self):
        self.n.problem("k", "x", T0)
        self.n.problem("k", "x", T0 + 10 * MIN)
        self.n.clear("k", "ok", T0 + 11 * MIN)
        self.n.not_delivered("k", T0 + 11 * MIN)  # the late failure report of the first message
        self.assertEqual(self.n.problems, {})


class PayloadTest(unittest.TestCase):
    def test_formats(self):
        m = Message("source:orgs/o", "text", resolved=True)
        self.assertEqual(payload("discord", m), {"content": "text", "allowed_mentions": {"parse": []}})
        self.assertEqual(payload("slack", m), {"text": "text"})
        self.assertEqual(payload("json", m), {"event": "source", "key": "source:orgs/o", "text": "text", "resolved": True})

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
            for url, ok in (("http://example.com/hook", False), ("http://127.0.0.1:9199/hook", True),
                            ("http://localhost/hook", True), ("ftp://example.com/x", False), ("https://", False)):
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
        s = Sender(r.url, "discord", "rackctl/test")
        s.send(Message("start", "hello"))
        s.close()
        self.assertEqual(r.bodies, [{"content": "hello", "allowed_mentions": {"parse": []}}])
        self.assertEqual(r.headers[0]["User-Agent"], "rackctl/test")
        self.assertEqual(s.failed, 0)

    def test_an_error_answer_is_counted_and_the_url_is_never_logged(self):
        r = Receiver(status=500)
        self.addCleanup(r.close)
        s = Sender(r.url, "json", "rackctl/test")
        with self.assertLogs("rackctl", level="INFO") as logs:
            s.send(Message("k", "x"))
            s.close()
        self.assertEqual(s.failed, 1)
        self.assertEqual(s.take_undelivered(), ["k"])  # handed back for a retry, once
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
        self.assertEqual((s.failed, s.take_undelivered()), (1, ["k"]))

    def test_an_unreachable_webhook_is_counted_and_the_url_is_never_logged(self):
        r = Receiver()
        url = r.url
        r.close()  # nothing listens on that port any more
        s = Sender(url, "json", "rackctl/test", timeout=2)
        with self.assertLogs("rackctl", level="INFO") as logs:
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
                {"notify": {"url_file": path, "format": "slack", "after_minutes": 3, "repeat_hours": 2}}, "v")
            self.addCleanup(sender.close)
        self.assertEqual((notifier.after, notifier.repeat), (3 * MIN, timedelta(hours=2)))


if __name__ == "__main__":
    unittest.main()
