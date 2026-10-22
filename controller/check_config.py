"""Check a controller configuration before it is installed, and list the paths it uses.

    python3 controller/check_config.py CONFIG.json

It loads the file the same way the controller does (``rackctl.main.load_config`` and
``machines_from``) and checks the type and form of the settings, sources and machines
(not every field: a machine's ``name`` and ``rank`` are taken as the controller reads them). Token
files are not read: on a fresh controller they do not exist yet.

The SSH key, ``known_hosts`` and every token file must sit directly in ``/etc/rackctl``:
the installer changes owners and modes of these files as root, so it must never be
pointed at a file elsewhere.

On success it prints one ``kind<TAB>value`` line per path the installer has to prepare:

    ssh_key      /etc/rackctl/id_ed25519
    known_hosts  /etc/rackctl/known_hosts
    token        /etc/rackctl/token-example-org
    token        /etc/rackctl/notify-url   (the webhook URL, when ``notify`` is set)
    host         192.0.2.11

On failure it prints the reason to stderr and exits with 1.
"""

import ipaddress
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from rackctl import actions, main  # noqa: E402  (needs the path above)
from rackctl.notify import FORMATS  # noqa: E402

ETC = "/etc/rackctl"
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HOST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
RUNNERS_PATH = re.compile(r"^(repos/[^/\s]+/[^/\s]+|orgs/[^/\s]+)$")
REPO = re.compile(r"^[^/\s]+/[^/\s]+$")


def in_etc(key: str, value) -> str:
    if not isinstance(value, str) or os.path.dirname(value) != ETC or not FILE_NAME.match(os.path.basename(value)):
        raise ValueError(f"{key} must be a file directly in {ETC}/ (letters, digits, '.', '_', '-')")
    return value


def positive_int(cfg: dict, key: str) -> None:
    v = cfg[key]
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"{key} must be a positive whole number")


def check(path: str):
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
        or "/etc/rackctl/config.json" in used.values()
    ):
        raise ValueError("ssh_key and known_hosts must be two files other than config.json")
    out.append(("ssh_key", used["ssh_key"]))
    out.append(("known_hosts", used["known_hosts"]))

    sources = cfg.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty list")
    for i, s in enumerate(sources):
        where = f"sources[{i}]"
        if not RUNNERS_PATH.match(str(s.get("runners_path", ""))):
            raise ValueError(f"{where}.runners_path must be repos/<owner>/<repo> or orgs/<org>")
        repos = s.get("repos")
        if not isinstance(repos, list) or not repos or not all(REPO.match(str(r)) for r in repos):
            raise ValueError(f"{where}.repos must be a non-empty list of <owner>/<repo>")
        token = in_etc(f"{where}.token_file", s.get("token_file"))
        # Several sources may share one token file; it must only not be another kind of file.
        if token in (used["ssh_key"], used["known_hosts"]) or os.path.basename(token) in ("config.json", os.path.basename(used["ssh_key"]) + ".pub"):
            raise ValueError(f"{where}.token_file must not reuse another file's name")
        out.append(("token", token))

    notify = cfg.get("notify")
    if notify is not None:
        if not isinstance(notify, dict):
            raise ValueError("notify must be an object")
        unknown = set(notify) - {"url_file", "format", "after_minutes", "repeat_hours"}
        if unknown:
            raise ValueError(f"notify has unknown keys: {', '.join(sorted(unknown))}")
        url_file = in_etc("notify.url_file", notify.get("url_file"))
        tokens = {v for k, v in out if k == "token"}
        if url_file in tokens | {used["ssh_key"], used["known_hosts"], used["ssh_key"] + ".pub", ETC + "/config.json"}:
            raise ValueError("notify.url_file must be a file of its own")
        if notify.get("format") not in FORMATS:
            raise ValueError(f"notify.format must be one of {', '.join(FORMATS)}")
        for key, default in (("after_minutes", 10), ("repeat_hours", 6)):
            v = notify.get(key, default)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (key == "repeat_hours" and v == 0):
                raise ValueError(f"notify.{key} must be a number, at least {'1' if key == 'repeat_hours' else '0'}")
        # The installer treats it like a token: it must exist and be readable by the service.
        out.append(("token", url_file))

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
            if not HOST_NAME.match(m.host):
                raise ValueError(f"machine {m.name}: host must be an address or a host name")
        if not re.match(r"^[a-z_][a-z0-9_-]*$", m.ssh_user):
            raise ValueError(f"machine {m.name}: ssh_user is not a valid user name")
        out.append(("host", m.host))
    return out


def run(argv) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    try:
        lines = check(argv[0])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"invalid configuration {argv[0]}: {e!r}", file=sys.stderr)
        return 1
    for kind, value in lines:
        print(f"{kind}\t{value}")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
