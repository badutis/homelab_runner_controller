"""Check a WakeCI configuration: the rules the controller and the installer rely on.

``wakeci check CONFIG`` runs it; ``controller/check_config.py`` (used by the installer and
``deploy.sh``) prints the paths the installer has to prepare, one ``kind<TAB>value`` line
each:

    ssh_key      /etc/wakeci/id_ed25519
    known_hosts  /etc/wakeci/known_hosts
    token        /etc/wakeci/token-example-org
    token        /etc/wakeci/notify-url   (the webhook URL, when ``notify`` is set)
    host         192.0.2.11

The file is loaded the same way the controller loads it (``main.load_config`` and
``machines_from``). Unknown keys are refused, so a typo is not silently ignored. Token
files are not read: on a fresh controller they do not exist yet.

The SSH key, ``known_hosts`` and every token file must sit directly in ``/etc/wakeci``: the
installer changes owners and modes of these files as root, so it must never be pointed at a
file elsewhere.

``docs/config.schema.json`` describes the same rules for editors; a test keeps the two in
step.
"""

import ipaddress
import json
import os
import re
import sys
from typing import List, Tuple

from . import actions, main
from .notify import FORMATS

CONFIG_VERSION = 1
TOP_KEYS = {
    "$schema",
    "config_version",
    "dry_run",
    "poll_seconds",
    "idle_minutes",
    "broadcast",
    "metrics_listen",
    "state_file",
    "ssh_key",
    "known_hosts",
    "notify",
    "sources",
    "machines",
}
SOURCE_KEYS = {"runners_path", "repos", "token_file"}
MACHINE_KEYS = {"name", "mac", "host", "ssh_user", "runner_prefixes", "rank"}
NOTIFY_KEYS = {"url_file", "format", "after_minutes", "repeat_hours"}

ETC = "/etc/wakeci"
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HOST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
RUNNERS_PATH = re.compile(r"^(repos/[^/\s]+/[^/\s]+|orgs/[^/\s]+)$")
REPO = re.compile(r"^[^/\s]+/[^/\s]+$")


def in_etc(key: str, value) -> str:
    if not isinstance(value, str) or os.path.dirname(value) != ETC or not FILE_NAME.fullmatch(os.path.basename(value)):
        raise ValueError(f"{key} must be a file directly in {ETC}/ (letters, digits, '.', '_', '-')")
    return value


def positive_int(cfg: dict, key: str) -> None:
    v = cfg[key]
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"{key} must be a positive whole number")


def no_unknown_keys(where: str, obj, allowed: set) -> None:
    if not isinstance(obj, dict):
        raise ValueError(f"{where} must be an object")
    unknown = set(obj) - allowed
    if unknown:
        raise ValueError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")


def check(path: str, warnings: "List[str] | None" = None) -> List[Tuple[str, str]]:
    """The paths to prepare, or ValueError. Notes that are not errors go to ``warnings``."""
    warnings = [] if warnings is None else warnings
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    no_unknown_keys("the configuration", raw, TOP_KEYS)
    if "config_version" not in raw:
        warnings.append(f'no config_version: read as version {CONFIG_VERSION}; add "config_version": {CONFIG_VERSION}')
    cfg = main.load_config(path)
    out = []
    if not isinstance(cfg["dry_run"], bool):
        raise ValueError("dry_run must be true or false")
    positive_int(cfg, "poll_seconds")
    positive_int(cfg, "idle_minutes")
    try:
        ipaddress.IPv4Address(cfg["broadcast"])
    except (ValueError, TypeError):
        raise ValueError("broadcast must be an IPv4 address")
    listen = cfg["metrics_listen"]
    host, _, port = listen.rpartition(":") if isinstance(listen, str) else ("", "", "")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("metrics_listen must be <address>:<port>")
    if not isinstance(cfg["state_file"], str) or not os.path.isabs(cfg["state_file"]):
        raise ValueError("state_file must be an absolute path")
    used = {
        "ssh_key": in_etc("ssh_key", cfg.get("ssh_key")),
        "known_hosts": in_etc("known_hosts", cfg["known_hosts"]),
    }
    if (
        used["ssh_key"] == used["known_hosts"]
        or used["known_hosts"] == used["ssh_key"] + ".pub"
        or "/etc/wakeci/config.json" in used.values()
    ):
        raise ValueError("ssh_key and known_hosts must be two files other than config.json")
    out.append(("ssh_key", used["ssh_key"]))
    out.append(("known_hosts", used["known_hosts"]))

    sources = cfg.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty list")
    for i, s in enumerate(sources):
        where = f"sources[{i}]"
        no_unknown_keys(where, s, SOURCE_KEYS)
        if not RUNNERS_PATH.fullmatch(str(s.get("runners_path", ""))):
            raise ValueError(f"{where}.runners_path must be repos/<owner>/<repo> or orgs/<org>")
        repos = s.get("repos")
        if not isinstance(repos, list) or not repos or not all(REPO.fullmatch(str(r)) for r in repos):
            raise ValueError(f"{where}.repos must be a non-empty list of <owner>/<repo>")
        token = in_etc(f"{where}.token_file", s.get("token_file"))
        # Several sources may share one token file; it must only not be another kind of file.
        if token in (used["ssh_key"], used["known_hosts"]) or os.path.basename(token) in (
            "config.json",
            os.path.basename(used["ssh_key"]) + ".pub",
        ):
            raise ValueError(f"{where}.token_file must not reuse another file's name")
        out.append(("token", token))

    notify = cfg.get("notify")
    if notify is not None:
        if not isinstance(notify, dict):
            raise ValueError("notify must be an object")
        no_unknown_keys("notify", notify, NOTIFY_KEYS)
        url_file = in_etc("notify.url_file", notify.get("url_file"))
        tokens = {v for k, v in out if k == "token"}
        if url_file in tokens | {used["ssh_key"], used["known_hosts"], used["ssh_key"] + ".pub", ETC + "/config.json"}:
            raise ValueError("notify.url_file must be a file of its own")
        if notify.get("format") not in FORMATS:
            raise ValueError(f"notify.format must be one of {', '.join(FORMATS)}")
        after = notify.get("after_minutes", 10)
        if isinstance(after, bool) or not isinstance(after, (int, float)) or after < 0:
            raise ValueError("notify.after_minutes must be a number, 0 or more")
        repeat = notify.get("repeat_hours", 6)
        if isinstance(repeat, bool) or not isinstance(repeat, (int, float)) or repeat <= 0:
            raise ValueError("notify.repeat_hours must be a number greater than 0")
        # The installer treats it like a token: it must exist and be readable by the service.
        out.append(("token", url_file))

    if not isinstance(cfg.get("machines"), list):
        raise ValueError("machines must be a list")
    for i, m in enumerate(cfg["machines"]):
        no_unknown_keys(f"machines[{i}]", m, MACHINE_KEYS)
    machines = main.machines_from(cfg)
    if not machines:
        raise ValueError("machines must not be empty")
    names = [m.name for m in machines]
    if len(set(names)) != len(names):
        raise ValueError("machine names must be unique")
    for m in machines:
        actions.magic_packet(m.mac)  # raises on a malformed MAC
        if not m.runner_prefixes or not all(isinstance(p, str) and p for p in m.runner_prefixes):
            raise ValueError(f"machine {m.name}: runner_prefixes must be non-empty strings")
        if not isinstance(m.host, str) or "%" in m.host:
            raise ValueError(f"machine {m.name}: host must be an address or a host name")
        try:
            ipaddress.ip_address(m.host)
        except ValueError:
            # Not starting with "-": the host is passed to ssh-keyscan and ssh.
            if not HOST_NAME.fullmatch(m.host):
                raise ValueError(f"machine {m.name}: host must be an address or a host name")
        rank = next((x.get("rank", 1) for x in cfg["machines"] if x.get("name") == m.name), 1)
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise ValueError(f"machine {m.name}: rank must be a whole number")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", m.ssh_user):
            raise ValueError(f"machine {m.name}: ssh_user is not a valid user name")
        out.append(("host", m.host))
    return out


def paths_main(argv: List[str]) -> int:
    """``controller/check_config.py CONFIG``: the path list for the installer."""
    if len(argv) != 1:
        print("usage: check_config.py CONFIG.json", file=sys.stderr)
        return 2
    warnings: List[str] = []
    try:
        lines = check(argv[0], warnings)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"invalid configuration {argv[0]}: {e!r}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for kind, value in lines:
        print(f"{kind}\t{value}")
    return 0
