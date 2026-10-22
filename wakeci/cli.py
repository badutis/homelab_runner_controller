"""The ``wakeci`` command.

    wakeci run CONFIG       run the controller (what the systemd unit starts)
    wakeci check CONFIG     check a configuration; exit 0 when it is valid
    wakeci version          print the version

``python3 -m wakeci CONFIG``, the form used before the subcommands existed, still runs the
controller.
"""

import argparse
import logging
import os
import sys
from typing import List, Optional

from . import __version__, config_check

COMMANDS = ("run", "check", "version")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wakeci",
        description="Wakes self-hosted GitHub Actions runner machines for queued jobs and suspends idle ones.",
    )
    ap.add_argument("--version", action="version", version=f"wakeci {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    run = sub.add_parser("run", help="run the controller")
    run.add_argument("config", help="path to config.json")
    check = sub.add_parser("check", help="check a configuration")
    check.add_argument("config", help="path to config.json")
    check.add_argument("--paths", action="store_true", help="print the paths the installer prepares")
    sub.add_parser("version", help="print the version")
    return ap


def _check(path: str, paths: bool) -> int:
    warnings: List[str] = []
    try:
        lines = config_check.check(path, warnings)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"wakeci: {path} is not valid: {e}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"wakeci: warning: {w}", file=sys.stderr)
    if paths:
        for kind, value in lines:
            print(f"{kind}\t{value}")
    else:
        machines = sum(1 for kind, _ in lines if kind == "host")
        print(f"{path}: valid ({machines} machines)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # The form before subcommands: `python3 -m wakeci CONFIG`. Only a word that looks like a
    # path is taken as one, so a misspelt command still gets argparse's "invalid choice".
    a = args[0] if len(args) == 1 else ""
    if a and a not in COMMANDS and not a.startswith("-") and ("/" in a or a.endswith(".json") or os.path.isfile(a)):
        args = ["run", a]
    ns = _parser().parse_args(args)
    if ns.command == "version":
        print(f"wakeci {__version__}")
        return 0
    if ns.command == "check":
        return _check(ns.config, ns.paths)
    from . import main as controller

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = controller.load_config(ns.config)
    if "config_version" not in cfg:
        logging.getLogger("wakeci").warning(
            '%s has no config_version: read as version %d; add "config_version": %d',
            ns.config,
            config_check.CONFIG_VERSION,
            config_check.CONFIG_VERSION,
        )
    controller.run(cfg)
    return 0
