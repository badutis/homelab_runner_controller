"""Valdiklio paleidimas: `python3 -m rackctl /etc/rackctl/config.json`.

Kas `poll_seconds` klausia GitHub, perduoda tai `Controller.step` ir įvykdo veiksmus.
Kol `dry_run` yra `true` (numatyta), veiksmai tik užrašomi į žurnalą.
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

log = logging.getLogger("rackctl")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("dry_run", True)
    cfg.setdefault("poll_seconds", 20)
    cfg.setdefault("idle_minutes", 30)
    cfg.setdefault("broadcast", "255.255.255.255")
    cfg.setdefault("metrics_listen", "0.0.0.0:9105")
    cfg.setdefault("state_file", "/var/lib/rackctl/last-online.json")
    cfg.setdefault("known_hosts", "/etc/rackctl/known_hosts")
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


def sources_from(cfg: dict) -> List[Source]:
    return [
        Source(runners_path=s["runners_path"], repos=tuple(s["repos"]), token=read_token(s["token_file"]))
        for s in cfg["sources"]
    ]


# --- būsena diske ---------------------------------------------------------------------
# Tik tai, ko negalima sužinoti iš GitHub: kada mašina paskutinį kartą buvo prisijungusi
# (14 dienų taisyklė) ir iki kada laikoma pabudusi po savaitinio pažadinimo. Rašoma tik
# pasikeitus, todėl SD kortelei tai keli įrašai per dieną.

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
    """Sugadintas ar svetimos formos failas — tas pats, kas jokio failo."""
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


# --- metrikos stebėjimui ------------------------------------------------------------


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
    ) -> None:
        lines = [
            "# HELP rackctl_machine_awake 1 when at least one runner of the machine is online.",
            "# TYPE rackctl_machine_awake gauge",
        ]
        for name, s in sorted(ctl.state.items()):
            lines.append(f'rackctl_machine_awake{{machine="{name}"}} {int(s.awake)}')
        lines += ["# TYPE rackctl_machine_waking gauge"]
        for name, s in sorted(ctl.state.items()):
            lines.append(f'rackctl_machine_waking{{machine="{name}"}} {int(s.wake_sent is not None)}')
        lines += ["# TYPE rackctl_actions_total counter"]
        for key, value in sorted(counters.items()):
            kind, machine = key.split("/", 1)
            lines.append(f'rackctl_actions_total{{kind="{kind}",machine="{machine}"}} {value}')
        lines += [
            "# HELP rackctl_runner_busy_seconds_total Seconds the runner was seen busy (20 s polls).",
            "# TYPE rackctl_runner_busy_seconds_total counter",
        ]
        for name, secs in sorted(busy_seconds.items()):
            lines.append(f'rackctl_runner_busy_seconds_total{{runner="{name}"}} {secs:.0f}')
        lines += ["# TYPE rackctl_queued_jobs gauge"]
        queued: Dict[str, int] = {}
        for job in obs.queued if obs else ():
            key = ",".join(sorted(job.labels - {"self-hosted"}))
            queued[key] = queued.get(key, 0) + 1
        for key, n in sorted(queued.items()):
            lines.append(f'rackctl_queued_jobs{{labels="{key}"}} {n}')
        # Kiek laukia seniausias eilės job'as: pranešimui „laukia per ilgai“ eilės ilgio
        # neužtenka — nuolat judanti eilė irgi ne tuščia.
        lines += ["# TYPE rackctl_queued_oldest_seconds gauge"]
        oldest: Dict[str, float] = {}
        now = datetime.now(timezone.utc)
        for job in obs.queued if obs else ():
            if job.since is None:
                continue
            key = ",".join(sorted(job.labels - {"self-hosted"}))
            oldest[key] = max(oldest.get(key, 0.0), (now - job.since).total_seconds())
        for key, secs in sorted(oldest.items()):
            lines.append(f'rackctl_queued_oldest_seconds{{labels="{key}"}} {secs:.0f}')
        lines += ["# TYPE rackctl_github_ok gauge"]
        for src in gh.sources:
            ok = int(src.runners_path not in gh.errors)
            lines.append(f'rackctl_github_ok{{source="{src.runners_path}"}} {ok}')
        lines += ["# TYPE rackctl_github_remaining gauge"]
        for source, rem in sorted(gh.remaining.items()):
            lines.append(f'rackctl_github_remaining{{source="{source}"}} {rem}')
        lines += ["# TYPE rackctl_dry_run gauge", f"rackctl_dry_run {int(dry_run)}"]
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
        return "paketas išsiųstas"
    if a.kind == "sleep":
        return act.put_to_sleep(m, cfg["ssh_key"], cfg["known_hosts"])
    return "užrašyta"


def run(cfg: dict) -> None:
    machines = machines_from(cfg)
    by_name = {m.name: m for m in machines}
    dry_run = bool(cfg["dry_run"])
    settings = Settings(idle=timedelta(minutes=cfg["idle_minutes"]))
    if dry_run:
        # Stebėjimo režime mašina neužmiega, todėl tas pats „migdyčiau“ kartotųsi dažnai.
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
    log.info("pradėta: %d mašinos, dry_run=%s", len(machines), dry_run)

    while True:
        now = datetime.now(timezone.utc)
        try:
            pause = gh.backoff_until()
            if pause is not None and pause > now:
                log.warning("GitHub limitas beveik baigėsi; laukiama iki %s", pause.isoformat())
            else:
                obs = gh.observe()
                for source, err in gh.errors.items():
                    log.warning("šaltinis %s neperskaitytas: %s", source, err)
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
                    except Exception as e:  # noqa: BLE001 — vienos mašinos klaida nestabdo kitų
                        result = f"klaida: {e!r}"
                    log.info("%s %s: %s — %s", a.kind, a.machine, a.reason, result)
                    if a.kind == "sleep" and not result.startswith(act.SLEEP_OK):
                        counters[f"sleep_failed/{a.machine}"] = counters.get(f"sleep_failed/{a.machine}", 0) + 1
                        if ctl.sleep_failed(a.machine, now):
                            log.error("alert %s: neužmiega jau 3 kartus iš eilės — %s", a.machine, result)
                current = snapshot(ctl.state)
                if _days(current) != _days(saved) or _holds(current) != _holds(saved):
                    save_state(cfg["state_file"], current)
                    saved = current
        except Exception:  # noqa: BLE001 — ciklas nekrenta: kitas bandymas po poll_seconds
            log.exception("ciklo klaida")
        try:
            metrics.update(ctl, counters, gh, busy_seconds, obs, dry_run)
        except Exception:  # noqa: BLE001
            log.exception("metrikų klaida")
        time.sleep(cfg["poll_seconds"])


def _days(data: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    return {k: v.get("last_online", "")[:10] for k, v in data.items()}


def _holds(data: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    return {k: v.get("hold_until", "") for k, v in data.items()}


def main(argv: List[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(argv) != 2:
        print("naudojimas: python3 -m rackctl /etc/rackctl/config.json", file=sys.stderr)
        return 2
    run(load_config(argv[1]))
    return 0
