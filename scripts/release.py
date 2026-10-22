"""Checks and notes for a release, run by .github/workflows/release.yml.

    python3 scripts/release.py check [--tag vX.Y.Z]
    python3 scripts/release.py notes VERSION

``check`` refuses to release unless:

- ``wakeci/__init__.py`` holds a plain version (X.Y.Z);
- CHANGELOG.md has a non-empty section for that version;
- with ``--tag``: the tag is exactly ``v`` + that version, is an annotated tag, and points
  at a commit that is on ``origin/main``.

Without ``--tag`` (a manual run of the workflow) it checks everything that does not need a
tag, so a release can be rehearsed before the tag is pushed.

``notes`` prints that version's CHANGELOG section, for the GitHub release.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.M)
PLAIN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def package_version(root: Path = ROOT) -> str:
    m = VERSION.search((root / "wakeci" / "__init__.py").read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("release: no __version__ in wakeci/__init__.py")
    return m.group(1)


def notes(version: str, root: Path = ROOT) -> str:
    """The text under ``## [VERSION]`` in CHANGELOG.md, up to the next ``## `` heading."""
    path = root / "CHANGELOG.md"
    if not path.exists():
        raise SystemExit("release: CHANGELOG.md is missing")
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    body = m.group(1).strip() if m else ""
    if not body:
        raise SystemExit(f"release: CHANGELOG.md has no section for {version}")
    return body


def git(*args: str, root: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def check(tag: Optional[str], root: Path = ROOT) -> str:
    version = package_version(root)
    if not PLAIN.fullmatch(version):
        raise SystemExit(f"release: {version} is not a release version (X.Y.Z)")
    notes(version, root)
    if tag is not None:
        if tag != f"v{version}":
            raise SystemExit(f"release: tag {tag} does not match the package version {version}")
        if git("cat-file", "-t", f"refs/tags/{tag}", root=root).stdout.strip() != "tag":
            raise SystemExit(f"release: {tag} is not an annotated tag")
        if git("merge-base", "--is-ancestor", f"{tag}^{{commit}}", "origin/main", root=root).returncode != 0:
            raise SystemExit(f"release: {tag} is not on origin/main")
    return version


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Checks and notes for a WakeCI release.")
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--tag")
    n = sub.add_parser("notes")
    n.add_argument("version")
    args = ap.parse_args(argv)
    if args.command == "check":
        print(f"release {check(args.tag)}: checks passed")
    else:
        print(notes(args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
