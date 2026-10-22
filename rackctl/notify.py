"""Notifications: a webhook message when something needs a person.

Two parts, like the rest of the controller:

- ``Notifier`` decides *when* to send, with no network and no clock of its own (the caller
  passes ``now``), so it is tested like ``decide.py``. A **problem** (a source GitHub cannot
  be read from, the rate limit) is reported once it has lasted ``after``, repeated every
  ``repeat`` while it lasts, and followed by one "resolved" message when it ends, if it was
  reported. A one-off **event** (a machine that did not wake up, the controller starting) is
  sent at most once per ``repeat`` for the same key.
- ``Sender`` posts the messages from its own thread through a bounded queue, so a slow or
  broken webhook never holds up the control loop. A failed post is logged and counted.

The webhook URL holds a secret (a Discord or Slack URL is enough to post as you), so it is
read from a file and never written to the log.
"""

import collections
import json
import logging
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Deque, Dict, List, Optional

log = logging.getLogger("rackctl")

FORMATS = ("discord", "slack", "json")
DISCORD_LIMIT = 2000  # characters in one Discord message
RETRY_AFTER = timedelta(minutes=5)  # a message that could not be sent is tried again after this
GIVE_UP_AFTER = timedelta(hours=24)  # ... until this long after its first failure
LOOPBACK = ("127.0.0.1", "::1", "localhost")


@dataclass(frozen=True)
class Message:
    key: str  # e.g. "source:orgs/example-org2", "wake:host1"
    text: str
    resolved: bool = False
    problem: bool = False  # about a problem that is still on (it may end before a retry)


@dataclass
class _Problem:
    since: datetime
    text: str
    last_sent: Optional[datetime] = None


@dataclass
class Notifier:
    after: timedelta = timedelta(minutes=10)
    repeat: timedelta = timedelta(hours=6)
    problems: Dict[str, _Problem] = field(default_factory=dict)
    events: Dict[str, datetime] = field(default_factory=dict)

    def problem(self, key: str, text: str, now: datetime, wait: bool = True) -> Optional[Message]:
        """The problem ``key`` is present at ``now``. ``wait=False`` reports it at once."""
        p = self.problems.get(key)
        if p is None:
            p = self.problems[key] = _Problem(since=now, text=text)
        p.text = text
        if p.last_sent is None:
            if wait and now - p.since < self.after:
                return None
        elif now - p.last_sent < self.repeat:
            return None
        p.last_sent = now
        return Message(key, text, problem=True)

    def clear(self, key: str, text: str, now: datetime) -> Optional[Message]:
        """The problem ``key`` is gone. A message only if it had been reported."""
        p = self.problems.pop(key, None)
        if p is None or p.last_sent is None:
            return None
        return Message(key, text, resolved=True)

    def event(self, key: str, text: str, now: datetime) -> Optional[Message]:
        last = self.events.get(key)
        if last is not None and now - last < self.repeat:
            return None
        self.events[key] = now
        return Message(key, text)

    def resent(self, key: str, now: datetime) -> None:
        """A retry of ``key`` went out at ``now``: the next repeat counts from here, so a
        retry that finally gets through is not followed at once by the regular repeat."""
        p = self.problems.get(key)
        if p is not None and p.last_sent is not None:
            p.last_sent = now
        if key in self.events:
            self.events[key] = now

    def reported(self, key: str) -> bool:
        """The problem ``key`` is on and has been reported."""
        p = self.problems.get(key)
        return p is not None and p.last_sent is not None


@dataclass
class Outbox:
    """Messages that were not delivered, sent again every ``delay`` until they are (for at
    most ``give_up``), unless they went stale meanwhile: the problem ended, a "resolved"
    message's problem came back and was reported, or a newer message with the same key was
    queued. A retry says when the message was first tried. Pure logic, like ``Notifier``."""

    delay: timedelta = RETRY_AFTER
    give_up: timedelta = GIVE_UP_AFTER
    latest: Dict[str, int] = field(default_factory=dict)  # key -> sequence number last queued
    # key -> (original message, next try, first failure)
    pending: Dict[str, "tuple[Message, datetime, datetime]"] = field(default_factory=dict)
    # key -> (original message, first failure, the retry copy that was handed out)
    retrying: Dict[str, "tuple[Message, datetime, Message]"] = field(default_factory=dict)

    def queued(self, msg: Message, seq: int) -> None:
        self.latest[msg.key] = seq
        self.pending.pop(msg.key, None)  # a newer message replaces a waiting retry
        r = self.retrying.get(msg.key)
        if r is not None and r[2] is not msg:
            del self.retrying[msg.key]  # a new message, not our retry

    def failed(self, msg: Message, seq: int, now: datetime) -> bool:
        """Note a failed post. True only for a message's first failure, so a caller can
        count messages rather than attempts."""
        if self.latest.get(msg.key) != seq:  # a failure of an older message is stale
            return False
        r = self.retrying.get(msg.key)
        if r is not None and r[2] is msg:
            self.pending[msg.key] = (r[0], now + self.delay, r[1])
            return False
        self.pending[msg.key] = (msg, now + self.delay, now)
        return True

    def due(self, now: datetime, notifier: Notifier) -> List[Message]:
        out = []
        for key, (msg, at, first) in list(self.pending.items()):
            if at > now:
                continue
            del self.pending[key]
            if now - first >= self.give_up:
                log.warning("notify %s: not delivered since %s; giving up", key, f"{first:%Y-%m-%d %H:%M} UTC")
                continue
            if msg.problem and key not in notifier.problems:
                continue  # the problem ended
            if msg.resolved and notifier.reported(key):
                continue  # the problem came back, and that has been reported
            if msg.resolved and key in notifier.problems:
                self.pending[key] = (msg, now + self.delay, first)  # a short flap: wait it out
                continue
            copy = Message(key, f"{msg.text} (delayed: first tried {first:%Y-%m-%d %H:%M} UTC)",
                           resolved=msg.resolved, problem=msg.problem)
            self.retrying[key] = (msg, first, copy)
            out.append(copy)
        return out


def payload(fmt: str, msg: Message) -> dict:
    if fmt == "discord":
        # No pings: a text taken from an error must not be able to mention @everyone.
        return {"content": msg.text[:DISCORD_LIMIT], "allowed_mentions": {"parse": []}}
    if fmt == "slack":
        return {"text": msg.text}
    return {"event": msg.key.split(":", 1)[0], "key": msg.key, "text": msg.text, "resolved": msg.resolved}


def read_url(path: str) -> str:
    """The webhook URL: https, or plain http only to this machine itself (a local relay)."""
    with open(path, encoding="utf-8") as f:
        url = f.read().strip()
    parts = urllib.parse.urlsplit(url)
    ok = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK)
    if not ok or not parts.hostname:
        # Says nothing about the content: the file holds a secret.
        raise ValueError(f"{path} does not hold an https URL (plain http only to localhost)")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect of a POST with a GET and no body, which looks like a
    success while nothing is posted. A redirect is a failure instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Sender:
    def __init__(self, url: str, fmt: str, user_agent: str, opener: Callable = _OPENER.open,
                 timeout: float = 10.0, size: int = 100):
        if fmt not in FORMATS:
            raise ValueError(f"unknown notify format {fmt!r}")
        self._url = url
        self._fmt = fmt
        self._ua = user_agent
        self._open = opener
        self._timeout = timeout
        self._queue: "queue.Queue[Optional[tuple[int, Message]]]" = queue.Queue(maxsize=size)
        self.failed = 0
        # (sequence number, message) of posts that were not delivered; the loop takes them.
        self._undelivered: Deque["tuple[int, Message]"] = collections.deque()
        self._seq = 0
        self._thread = threading.Thread(target=self._run, name="rackctl-notify", daemon=True)
        self._thread.start()

    def send(self, msg: Message) -> int:
        """Queue ``msg``; returns its sequence number (see Outbox)."""
        self._seq += 1
        seq = self._seq
        log.info("notify %s: %s", msg.key, msg.text)
        try:
            self._queue.put_nowait((seq, msg))
        except queue.Full:
            self._fail(seq, msg, "queue full")
        return seq

    def take_undelivered(self) -> List["tuple[int, Message]"]:
        out = []
        while self._undelivered:
            out.append(self._undelivered.popleft())
        return out

    def _fail(self, seq: int, msg: Message, why: str) -> None:
        self.failed += 1
        self._undelivered.append((seq, msg))
        log.warning("notify %s: %s; trying again later", msg.key, why)

    def close(self, wait: float = 5.0) -> None:
        """Stop after the queued messages are sent (for tests and a clean exit)."""
        self._queue.put(None)
        self._thread.join(wait)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._post(*item)

    def _post(self, seq: int, msg: Message) -> None:
        body = json.dumps(payload(self._fmt, msg)).encode("utf-8")
        req = urllib.request.Request(self._url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", self._ua)  # some webhook hosts refuse urllib's default
        try:
            with self._open(req, timeout=self._timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            # 3xx (a redirect is refused, see _NoRedirect), 429 (rate limit), 5xx, ...
            self._fail(seq, msg, f"webhook answered HTTP {e.code}")
        except Exception as e:  # noqa: BLE001 — a broken webhook must never stop the controller
            # The exception text may contain the URL; only its type is logged.
            self._fail(seq, msg, f"post failed ({type(e).__name__})")
