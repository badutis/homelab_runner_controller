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

import json
import logging
import queue
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

log = logging.getLogger("rackctl")

FORMATS = ("discord", "slack", "json")
DISCORD_LIMIT = 2000  # characters in one Discord message


@dataclass(frozen=True)
class Message:
    key: str  # e.g. "source:orgs/example-org2", "wake:host1"
    text: str
    resolved: bool = False


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
        return Message(key, text)

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


def payload(fmt: str, msg: Message) -> dict:
    if fmt == "discord":
        # No pings: a text taken from an error must not be able to mention @everyone.
        return {"content": msg.text[:DISCORD_LIMIT], "allowed_mentions": {"parse": []}}
    if fmt == "slack":
        return {"text": msg.text}
    return {"event": msg.key.split(":", 1)[0], "key": msg.key, "text": msg.text, "resolved": msg.resolved}


def read_url(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        url = f.read().strip()
    if not (url.startswith("https://") or url.startswith("http://")):
        # Says nothing about the content: the file holds a secret.
        raise ValueError(f"{path} does not hold an http(s) URL")
    return url


class Sender:
    def __init__(self, url: str, fmt: str, user_agent: str, opener: Callable = urllib.request.urlopen,
                 timeout: float = 10.0, size: int = 100):
        if fmt not in FORMATS:
            raise ValueError(f"unknown notify format {fmt!r}")
        self._url = url
        self._fmt = fmt
        self._ua = user_agent
        self._open = opener
        self._timeout = timeout
        self._queue: "queue.Queue[Optional[Message]]" = queue.Queue(maxsize=size)
        self.failed = 0
        self._thread = threading.Thread(target=self._run, name="rackctl-notify", daemon=True)
        self._thread.start()

    def send(self, msg: Optional[Message]) -> None:
        if msg is None:
            return
        log.info("notify %s: %s", msg.key, msg.text)
        try:
            self._queue.put_nowait(msg)
        except queue.Full:
            self.failed += 1
            log.warning("notify %s: queue full, message dropped", msg.key)

    def close(self, wait: float = 5.0) -> None:
        """Stop after the queued messages are sent (for tests and a clean exit)."""
        self._queue.put(None)
        self._thread.join(wait)

    def _run(self) -> None:
        while True:
            msg = self._queue.get()
            if msg is None:
                return
            self._post(msg)

    def _post(self, msg: Message) -> None:
        body = json.dumps(payload(self._fmt, msg)).encode("utf-8")
        req = urllib.request.Request(self._url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", self._ua)  # some webhook hosts refuse urllib's default
        try:
            with self._open(req, timeout=self._timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            self.failed += 1
            log.warning("notify %s: webhook answered HTTP %s", msg.key, e.code)
        except Exception as e:  # noqa: BLE001 — a broken webhook must never stop the controller
            self.failed += 1
            # The exception text may contain the URL; only its type is logged.
            log.warning("notify %s: post failed (%s)", msg.key, type(e).__name__)
