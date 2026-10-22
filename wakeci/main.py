"""Running the controller: `python3 -m wakeci /etc/wakeci/config.json`.

Every `poll_seconds` it asks GitHub, passes the answer to `Controller.step` and carries
out the actions. While `dry_run` is `true` (the default), actions are only logged.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

from . import actions as act
from .decide import Action, Controller, Machine, MachineState, Observation, Settings
from .github import GitHub, Source, read_token
from .notify import Notifier, Outbox, Sender, read_url

log = logging.getLogger("wakeci")


SUPPORTED_CONFIG_VERSIONS = (1,)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    version = cfg.get("config_version", 1)
    if type(version) is not int or version not in SUPPORTED_CONFIG_VERSIONS:
        raise ValueError(
            f"{path}: config_version {version!r} is not supported by this WakeCI "
            f"(supported: {', '.join(map(str, SUPPORTED_CONFIG_VERSIONS))})"
        )
    cfg.setdefault("dry_run", True)
    cfg.setdefault("poll_seconds", 20)
    cfg.setdefault("idle_minutes", 30)
    cfg.setdefault("broadcast", "255.255.255.255")
    cfg.setdefault("metrics_listen", "0.0.0.0:9105")
    cfg.setdefault("state_file", "/var/lib/wakeci/last-online.json")
    cfg.setdefault("known_hosts", "/etc/wakeci/known_hosts")
    return cfg


def machines_from(cfg: dict) -> tuple:
    return tuple(
        Machine(
            name=m["name"],
            mac=m["mac"],
            host=m["host"],
            ssh_user=m.get("ssh_user", m["name"]),
            runner_prefixes=tuple(m["runner_prefixes"]),
            rank=int(m.get("rank", 1)),
        )
        for m in cfg["machines"]
    )


def read_version() -> str:
    """The release this copy was installed from (controller/install.sh writes VERSION next
    to the package, e.g. "v0.4.0"), else the package version."""
    from . import __version__

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or __version__
    except OSError:
        return __version__


def notifications(cfg: dict, version: str) -> "tuple[Notifier, Optional[Sender]]":
    """Without a `notify` block everything stays in the log, as before."""
    n = cfg.get("notify")
    if not n:
        return Notifier(), None
    notifier = Notifier(
        after=timedelta(minutes=n.get("after_minutes", 10)),
        repeat=timedelta(hours=n.get("repeat_hours", 6)),
    )
    return notifier, Sender(read_url(n["url_file"]), n["format"], f"wakeci/{version}")


def sources_from(cfg: dict) -> List[Source]:
    return [
        Source(runners_path=s["runners_path"], repos=tuple(s["repos"]), token=read_token(s["token_file"]))
        for s in cfg["sources"]
    ]


# --- state on disk --------------------------------------------------------------------
# Only what cannot be learned from GitHub: when each machine was last online (the 14-day
# rule) and until when it is held awake after a weekly wake. Written only on change: a
# few writes a day, which is gentle on an SD card.

PERSISTED = ("last_online", "hold_until")


def _aware(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def load_state(path: str) -> Dict[str, Dict[str, datetime]]:
    """A damaged or foreign file counts the same as no file."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, datetime]] = {}
    for name, fields in raw.items():
        if not isinstance(fields, dict):
            continue
        parsed = {k: _aware(fields.get(k)) for k in PERSISTED}
        out[name] = {k: v for k, v in parsed.items() if v is not None}
    return out


def snapshot(state: Dict[str, MachineState]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for name, s in state.items():
        fields = {k: getattr(s, k).isoformat() for k in PERSISTED if getattr(s, k) is not None}
        if fields:
            out[name] = fields
    return out


def save_state(path: str, data: Dict[str, Dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


# --- metrics for monitoring -----------------------------------------------------------


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.text = ""

    def update(
        self,
        ctl: Controller,
        counters: Dict[str, int],
        gh: "GitHub",
        busy_seconds: Dict[str, float],
        obs: Optional[Observation],
        dry_run: bool,
        notify_failed: int = 0,
    ) -> None:
        lines = [
            "# HELP wakeci_machine_awake 1 when at least one runner of the machine is online.",
            "# TYPE wakeci_machine_awake gauge",
        ]
        for name, s in sorted(ctl.state.items()):
            lines.append(f'wakeci_machine_awake{{machine="{name}"}} {int(s.awake)}')
        lines += ["# TYPE wakeci_machine_waking gauge"]
        for name, s in sorted(ctl.state.items()):
            lines.append(f'wakeci_machine_waking{{machine="{name}"}} {int(s.wake_sent is not None)}')
        lines += ["# TYPE wakeci_actions_total counter"]
        for key, value in sorted(counters.items()):
            kind, machine = key.split("/", 1)
            lines.append(f'wakeci_actions_total{{kind="{kind}",machine="{machine}"}} {value}')
        lines += [
            "# HELP wakeci_runner_busy_seconds_total Seconds the runner was seen busy (20 s polls).",
            "# TYPE wakeci_runner_busy_seconds_total counter",
        ]
        for name, secs in sorted(busy_seconds.items()):
            lines.append(f'wakeci_runner_busy_seconds_total{{runner="{name}"}} {secs:.0f}')
        lines += ["# TYPE wakeci_queued_jobs gauge"]
        queued: Dict[str, int] = {}
        for job in obs.queued if obs else ():
            key = ",".join(sorted(job.labels - {"self-hosted"}))
            queued[key] = queued.get(key, 0) + 1
        for key, n in sorted(queued.items()):
            lines.append(f'wakeci_queued_jobs{{labels="{key}"}} {n}')
        # How long the oldest queued job has waited: the queue length is not enough for a
        # "waiting too long" alert, because a queue that keeps moving is not empty either.
        lines += ["# TYPE wakeci_queued_oldest_seconds gauge"]
        oldest: Dict[str, float] = {}
        now = datetime.now(timezone.utc)
        for job in obs.queued if obs else ():
            if job.since is None:
                continue
            key = ",".join(sorted(job.labels - {"self-hosted"}))
            oldest[key] = max(oldest.get(key, 0.0), (now - job.since).total_seconds())
        for key, secs in sorted(oldest.items()):
            lines.append(f'wakeci_queued_oldest_seconds{{labels="{key}"}} {secs:.0f}')
        lines += ["# TYPE wakeci_github_ok gauge"]
        for src in gh.sources:
            ok = int(src.runners_path not in gh.errors)
            lines.append(f'wakeci_github_ok{{source="{src.runners_path}"}} {ok}')
        lines += ["# TYPE wakeci_github_remaining gauge"]
        for source, rem in sorted(gh.remaining.items()):
            lines.append(f'wakeci_github_remaining{{source="{source}"}} {rem}')
        lines += ["# TYPE wakeci_dry_run gauge", f"wakeci_dry_run {int(dry_run)}"]
        lines += [
            "# HELP wakeci_notify_failed_total Webhook messages whose post failed (retries not counted).",
            "# TYPE wakeci_notify_failed_total counter",
            f"wakeci_notify_failed_total {notify_failed}",
        ]
        with self.lock:
            self.text = "\n".join(lines) + "\n"

    def serve(self, listen: str) -> None:
        host, port = listen.rsplit(":", 1)
        metrics = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/metrics":
                    self.send_error(404)
                    return
                with metrics.lock:
                    body = metrics.text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer((host, int(port)), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()


# --- ciklas ------------------------------------------------------------------------------


def execute(a: Action, by_name: Dict[str, Machine], cfg: dict) -> str:
    m = by_name[a.machine]
    if a.kind == "wake":
        act.wake(m, cfg["broadcast"])
        return "packet sent"
    if a.kind == "sleep":
        return act.put_to_sleep(m, cfg["ssh_key"], cfg["known_hosts"])
    return "logged"


def run(cfg: dict) -> None:
    machines = machines_from(cfg)
    by_name = {m.name: m for m in machines}
    dry_run = bool(cfg["dry_run"])
    settings = Settings(idle=timedelta(minutes=cfg["idle_minutes"]))
    if dry_run:
        # In dry run the machine never goes to sleep, so the same "would suspend" would repeat often.
        settings = Settings(idle=settings.idle, sleep_retry=timedelta(hours=1))
    ctl = Controller(machines=machines, settings=settings, dry_run=dry_run)
    for name, fields in load_state(cfg["state_file"]).items():
        if name in ctl.state:
            for key, value in fields.items():
                setattr(ctl.state[name], key, value)
    gh = GitHub(sources_from(cfg))
    metrics = Metrics()
    metrics.serve(cfg["metrics_listen"])
    counters: Dict[str, int] = {}
    busy_seconds: Dict[str, float] = {}
    saved = snapshot(ctl.state)
    last_poll: Optional[datetime] = None
    obs: Optional[Observation] = None
    version = read_version()
    notifier, sender = notifications(cfg, version)

    outbox = Outbox()
    notify_failed = 0

    def notify(msg) -> None:
        if sender is not None and msg is not None:
            outbox.queued(msg, sender.send(msg))

    def retry_undelivered(now: datetime) -> None:
        nonlocal notify_failed
        if sender is None:
            return
        for seq, msg in sender.take_undelivered():
            if outbox.failed(msg, seq, now):
                notify_failed += 1
        for msg in outbox.due(now, notifier):
            notifier.resent(msg.key, now)
            notify(msg)

    log.info("started %s: %d machines, dry_run=%s", version, len(machines), dry_run)
    notify(
        notifier.event(
            "start",
            f"wakeci {version} started: {len(machines)} machines, dry_run={str(dry_run).lower()}.",
            datetime.now(timezone.utc),
        )
    )

    while True:
        now = datetime.now(timezone.utc)
        retry_undelivered(now)
        try:
            pause = gh.backoff_until()
            if pause is not None and pause > now:
                log.warning("GitHub limit nearly used up; waiting until %s", pause.isoformat())
                notify(
                    notifier.problem(
                        "limit",
                        f"wakeci: GitHub request limit nearly used up; not polling until "
                        f"{pause.strftime('%Y-%m-%d %H:%M UTC')}. Machines are neither woken nor suspended "
                        "meanwhile.",
                        now,
                        wait=False,
                    )
                )
            else:
                notify(notifier.clear("limit", "wakeci: GitHub request limit reset; polling again.", now))
                obs = gh.observe()
                for source, err in gh.errors.items():
                    log.warning("source %s not read: %s", source, err)
                for src in gh.sources:
                    key = f"source:{src.runners_path}"
                    err = gh.errors.get(src.runners_path)
                    if err is not None:
                        notify(
                            notifier.problem(
                                key,
                                f"wakeci: GitHub source {src.runners_path} cannot be read: {err}. "
                                "No machine is suspended while this lasts (check its token).",
                                now,
                            )
                        )
                    else:
                        notify(notifier.clear(key, f"wakeci: GitHub source {src.runners_path} is readable again.", now))
                if last_poll is not None:
                    elapsed = min((now - last_poll).total_seconds(), 2 * cfg["poll_seconds"])
                    for r in obs.runners:
                        if r.busy:
                            busy_seconds[r.name] = busy_seconds.get(r.name, 0.0) + elapsed
                last_poll = now
                for a in ctl.step(obs, now):
                    key = f"{a.kind}/{a.machine}"
                    counters[key] = counters.get(key, 0) + 1
                    if dry_run and a.kind != "alert":
                        log.info("[dry-run] %s %s: %s", a.kind, a.machine, a.reason)
                        continue
                    try:
                        result = execute(a, by_name, cfg)
                    except Exception as e:  # noqa: BLE001 — an error on one machine does not stop the others
                        result = f"error: {e!r}"
                    log.info("%s %s: %s — %s", a.kind, a.machine, a.reason, result)
                    if a.kind == "alert":
                        notify(notifier.event(f"wake:{a.machine}", f"wakeci: {a.machine} {a.reason}.", now))
                    if a.kind == "sleep" and not result.startswith(act.SLEEP_OK):
                        counters[f"sleep_failed/{a.machine}"] = counters.get(f"sleep_failed/{a.machine}", 0) + 1
                        if ctl.sleep_failed(a.machine, now):
                            log.error("alert %s: failed to suspend 3 times in a row — %s", a.machine, result)
                            notify(
                                notifier.event(
                                    f"sleep:{a.machine}",
                                    f"wakeci: {a.machine} failed to suspend 3 times in a row: {result}.",
                                    now,
                                )
                            )
                current = snapshot(ctl.state)
                if _days(current) != _days(saved) or _holds(current) != _holds(saved):
                    save_state(cfg["state_file"], current)
                    saved = current
        except Exception:  # noqa: BLE001 — the loop never dies: next try after poll_seconds
            log.exception("loop error")
        try:
            metrics.update(ctl, counters, gh, busy_seconds, obs, dry_run, notify_failed)
        except Exception:  # noqa: BLE001
            log.exception("metrics error")
        time.sleep(cfg["poll_seconds"])


def _days(data: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    return {k: v.get("last_online", "")[:10] for k, v in data.items()}


def _holds(data: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    return {k: v.get("hold_until", "") for k, v in data.items()}


def main(argv: List[str]) -> int:
    """Kept for callers of the old entry point; the command line lives in cli.py."""
    from .cli import main as cli_main

    return cli_main(argv[1:])
