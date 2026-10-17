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
from typing import Dict, List

from . import actions as act
from .decide import Action, Controller, Machine, Settings
from .github import GitHub, Source, first_error, read_token

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


# --- paskutinio prisijungimo laikas diske --------------------------------------------
# Rašoma tik kai pasikeičia diena, todėl SD kortelei tai keli įrašai per dieną.


def load_last_online(path: str) -> Dict[str, datetime]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: datetime.fromisoformat(v) for k, v in raw.items()}


def save_last_online(path: str, values: Dict[str, datetime]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: v.isoformat() for k, v in values.items()}, f)
    os.replace(tmp, path)


# --- metrikos MKS Pi -------------------------------------------------------------------


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.text = ""

    def update(self, ctl: Controller, counters: Dict[str, int], ok: bool, dry_run: bool) -> None:
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
            "# TYPE rackctl_github_ok gauge",
            f"rackctl_github_ok {int(ok)}",
            "# TYPE rackctl_dry_run gauge",
            f"rackctl_dry_run {int(dry_run)}",
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
        return "paketas išsiųstas"
    if a.kind == "sleep":
        return act.put_to_sleep(m, cfg["ssh_key"], cfg["known_hosts"])
    return "užrašyta"


def run(cfg: dict) -> None:
    machines = machines_from(cfg)
    by_name = {m.name: m for m in machines}
    ctl = Controller(machines=machines, settings=Settings(idle=timedelta(minutes=cfg["idle_minutes"])))
    saved = load_last_online(cfg["state_file"])
    for name, when in saved.items():
        if name in ctl.state:
            ctl.state[name].last_online = when
    gh = GitHub(sources_from(cfg))
    metrics = Metrics()
    metrics.serve(cfg["metrics_listen"])
    counters: Dict[str, int] = {}
    saved_days = {k: v.date() for k, v in saved.items()}
    log.info("pradėta: %d mašinos, dry_run=%s", len(machines), cfg["dry_run"])

    while True:
        now = datetime.now(timezone.utc)
        ok = True
        try:
            obs = gh.observe()
        except Exception as e:  # noqa: BLE001 — tinklas ar GitHub; bandoma kitame cikle
            ok = False
            log.warning("GitHub neatsakė: %s", first_error(e) or repr(e))
        if ok:
            for a in ctl.step(obs, now):
                key = f"{a.kind}/{a.machine}"
                counters[key] = counters.get(key, 0) + 1
                if cfg["dry_run"] and a.kind != "alert":
                    log.info("[dry-run] %s %s: %s", a.kind, a.machine, a.reason)
                    continue
                try:
                    result = execute(a, by_name, cfg)
                except Exception as e:  # noqa: BLE001 — vienos mašinos klaida nestabdo kitų
                    result = f"klaida: {e!r}"
                log.info("%s %s: %s — %s", a.kind, a.machine, a.reason, result)
            days = {n: s.last_online.date() for n, s in ctl.state.items() if s.last_online}
            if days != saved_days:
                save_last_online(
                    cfg["state_file"],
                    {n: s.last_online for n, s in ctl.state.items() if s.last_online},
                )
                saved_days = days
        metrics.update(ctl, counters, ok, cfg["dry_run"])
        time.sleep(cfg["poll_seconds"])


def main(argv: List[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(argv) != 2:
        print("naudojimas: python3 -m rackctl /etc/rackctl/config.json", file=sys.stderr)
        return 2
    run(load_config(argv[1]))
    return 0
