"""Every commit of a pull request carries a Developer Certificate of Origin sign-off.

    python3 scripts/dco.py BASE HEAD

Checks each non-merge commit in BASE..HEAD for a ``Signed-off-by: Name <email>`` trailer
whose email is the commit author's (``git commit -s`` writes exactly that). Exits 1 and
names the commits that lack it. See CONTRIBUTING.md.

Dependabot's commits are exempt: a bot cannot certify the DCO, and its pull requests only
move pinned versions that CI then checks.
"""

import re
import subprocess
import sys

SIGNED = re.compile(r"^Signed-off-by: .+ <([^<>\s]+)>\s*$", re.M)
EXEMPT = {"49699333+dependabot[bot]@users.noreply.github.com"}


def unsigned(base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--no-merges", "--format=%H%x00%ae%x00%B%x1e", f"{base}..{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    missing = []
    for entry in out.split("\x1e"):
        entry = entry.strip("\n")
        if not entry:
            continue
        sha, email, body = entry.split("\0", 2)
        if email in EXEMPT:
            continue
        if email.lower() not in {m.lower() for m in SIGNED.findall(body)}:
            missing.append(f"{sha[:12]} ({email})")
    return missing


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not all(re.fullmatch(r"[0-9a-f]{7,64}", a) for a in argv):
        print("usage: dco.py BASE_SHA HEAD_SHA", file=sys.stderr)
        return 2
    missing = unsigned(*argv)
    for m in missing:
        print(f"commit {m} has no Signed-off-by for its author (use `git commit -s`)")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
