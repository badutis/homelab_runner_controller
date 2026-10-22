"""Check a controller configuration before it is installed, and list the paths it uses.

    python3 orangepi/check_config.py CONFIG.json

It loads the file the same way the controller does (``rackctl.main.load_config`` and
``machines_from``), so a file that passes here also starts there. Token files are not
read: on a fresh controller they do not exist yet.

On success it prints one ``kind<TAB>value`` line per path the installer has to prepare:

    ssh_key      /etc/rackctl/id_ed25519
    known_hosts  /etc/rackctl/known_hosts
    token        /etc/rackctl/token-example-org
    host         192.0.2.11

On failure it prints the reason to stderr and exits with 1.
"""

import ipaddress
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from rackctl import actions, main  # noqa: E402  (needs the path above)

RUNNERS_PATH = re.compile(r"^(repos/[^/\s]+/[^/\s]+|orgs/[^/\s]+)$")
REPO = re.compile(r"^[^/\s]+/[^/\s]+$")


def check(path: str):
    cfg = main.load_config(path)
    out = []
    if not isinstance(cfg.get("ssh_key"), str) or not os.path.isabs(cfg["ssh_key"]):
        raise ValueError("ssh_key must be an absolute path")
    out.append(("ssh_key", cfg["ssh_key"]))
    if not os.path.isabs(cfg["known_hosts"]):
        raise ValueError("known_hosts must be an absolute path")
    out.append(("known_hosts", cfg["known_hosts"]))

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
        token = s.get("token_file")
        if not isinstance(token, str) or not os.path.isabs(token):
            raise ValueError(f"{where}.token_file must be an absolute path")
        out.append(("token", token))

    machines = main.machines_from(cfg)
    if not machines:
        raise ValueError("machines must not be empty")
    names = [m.name for m in machines]
    if len(set(names)) != len(names):
        raise ValueError("machine names must be unique")
    for m in machines:
        actions.magic_packet(m.mac)  # raises on a malformed MAC
        if not m.runner_prefixes or not all(m.runner_prefixes):
            raise ValueError(f"machine {m.name}: runner_prefixes must be non-empty strings")
        try:
            ipaddress.ip_address(m.host)
        except ValueError:
            if not re.match(r"^[A-Za-z0-9.-]+$", m.host):
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
