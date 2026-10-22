"""Refuse values that would tie this repository to one particular installation.

The repository must be safe to make public at any time. This catches the leaks that have
a recognisable shape:

- private and link-local IPv4 addresses, and IPv6 unique-local and link-local addresses;
- MAC addresses, written with ``:`` or ``-`` or in the dotted ``xxxx.xxxx.xxxx`` form;
- email addresses other than placeholders;
- names of AI tools, as attribution.

It checks every file in the index. With ``--history`` it also checks every version of
every file in the history of ``HEAD``, every commit message, author and committer, and
every tag's message and tagger.

Allowed placeholders: the documentation address ranges (RFC 5737: 192.0.2.0/24,
198.51.100.0/24, 203.0.113.0/24; RFC 3849: 2001:db8::/32), the documentation MAC range
(RFC 7042: 00:00:5e:00:53:xx), ``example.com/.org/.net`` and GitHub ``noreply``
addresses.

Not caught: public addresses, MAC addresses written as 12 hex digits without separators
(they cannot be told apart from hashes), UTF-16 files (read as binary), commits that only
a tag points at, and names of real machines, users or
repositories. A public list of those would itself leak them, so keeping them out stays
the author's job.

Exit code 0 when clean, 1 with one line per finding otherwise.
"""

import argparse
import ipaddress
import re
import subprocess
import sys
from typing import Iterable, Iterator, List, Optional, Tuple

# This file spells the patterns out, so it is not scanned. Its test builds its bad samples
# at run time and is scanned like any other file.
SELF = {"scripts/public_safe.py"}

PRIVATE_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
]
# A trailing dot ends a sentence and is allowed; a dot followed by a digit is not.
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\d)(?!\.\d)")
IPV6 = re.compile(r"(?<![0-9A-Fa-f])(?<![0-9A-Fa-f]:)(?<!::)((?:f[c-e][0-9a-f]{0,2}):[0-9a-f:]*[0-9a-f])(?![0-9a-f:])", re.I)
# A run of six or more hex pairs: the text right before a MAC may itself look like a
# pair ("mac:fc:..."), so the run is reported unless some six-pair window of it is allowed.
# Not after a 3–4 digit group and a colon: that is the tail of an IPv6 address.
MAC_RUN = re.compile(r"(?<![0-9A-Fa-f])(?<![0-9A-Fa-f]{3}:)((?:[0-9A-Fa-f]{2}[:-]){5,}[0-9A-Fa-f]{2})(?![0-9A-Fa-f])")
MAC_DOTTED = re.compile(r"(?<![0-9A-Fa-f.])([0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4})(?![0-9A-Fa-f])(?!\.[0-9A-Fa-f])")
MAC_ALLOWED = re.compile(r"^(00005e0053[0-9a-f]{2}|f{12}|0{11}[0-9a-f])$")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_ALLOWED = re.compile(r"(@users\.noreply\.github\.com|^noreply@github\.com|@example\.(com|org|net))$", re.I)
# Letters only around the name, so "claude_code" and "ClaudeCode" are caught too.
AI = re.compile(r"(?<![A-Za-z])(claude|anthropic|chatgpt|openai|copilot)", re.I)


def _ipv4(text: str) -> Optional[ipaddress.IPv4Address]:
    parts = text.split(".")
    if any(int(p) > 255 for p in parts):
        return None
    # "192.168.001.010" is refused by ipaddress, but it is still that address.
    return ipaddress.IPv4Address(".".join(str(int(p)) for p in parts))


def _mac_allowed(run: str) -> bool:
    pairs = re.split(r"[:-]", run.lower())
    return any(MAC_ALLOWED.match("".join(pairs[i : i + 6])) for i in range(len(pairs) - 5))


def findings_in_text(text: str) -> Iterator[str]:
    for m in IPV4.finditer(text):
        ip = _ipv4(m.group(1))
        if ip is not None and any(ip in net for net in PRIVATE_NETS):
            yield f"private address {ip}"
    for m in IPV6.finditer(text):
        try:
            ip6 = ipaddress.IPv6Address(m.group(1))
        except ValueError:
            continue
        if any(ip6 in net for net in PRIVATE_NETS):
            yield f"private address {ip6}"
    for m in MAC_RUN.finditer(text):
        if not _mac_allowed(m.group(1)):
            yield f"MAC address {m.group(1)}"
    for m in MAC_DOTTED.finditer(text):
        if not MAC_ALLOWED.match(m.group(1).replace(".", "").lower()):
            yield f"MAC address {m.group(1)}"
    for m in EMAIL.finditer(text):
        if not EMAIL_ALLOWED.search(m.group(0)):
            yield f"email address {m.group(0)}"
    for m in AI.finditer(text):
        yield f"AI attribution '{m.group(0)}'"


def scan_lines(where: str, text: str) -> List[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        out += [f"{where}:{n}: {f}" for f in findings_in_text(line)]
    return out


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def decode(raw: bytes) -> Optional[str]:
    """Text in any encoding is read; only a file with a NUL byte counts as binary."""
    if b"\0" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def index_files() -> Iterable[Tuple[str, str]]:
    # The committed content, not the working tree: a symlink is read as its target path,
    # a submodule (mode 160000) is skipped, a file deleted from disk does not crash.
    for entry in git("ls-files", "-s", "-z").decode(errors="replace").split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, sha = meta.split()[:2]
        if path in SELF or mode == "160000":
            continue
        text = decode(git("cat-file", "blob", sha))
        if text is not None:
            yield path, text


def history_blobs() -> Iterable[Tuple[str, str]]:
    seen = set()
    for line in git("rev-list", "--objects", "HEAD").decode(errors="replace").splitlines():
        sha, _, path = line.partition(" ")
        if not path or path in SELF or sha in seen:
            continue
        seen.add(sha)
        if git("cat-file", "-t", sha).strip() != b"blob":
            continue
        text = decode(git("cat-file", "blob", sha))
        if text is not None:
            yield f"{path}@{sha[:7]}", text


def commits() -> Iterable[Tuple[str, str]]:
    sep = "\x1e"
    log = git("log", "HEAD", f"--format=%h%x00%an <%ae>%n%cn <%ce>%x00%B{sep}").decode(errors="replace")
    for entry in log.split(sep):
        entry = entry.strip("\n")
        if not entry:
            continue
        sha, people, body = entry.split("\0", 2)
        yield f"commit {sha} (author, committer)", people
        yield f"commit {sha} (message)", body


def tags() -> Iterable[Tuple[str, str]]:
    sep = "\x1e"
    out = git("for-each-ref", "refs/tags", f"--format=%(refname:short)%00%(taggername) %(taggeremail)%00%(contents){sep}")
    for entry in out.decode(errors="replace").split(sep):
        entry = entry.strip("\n")
        if not entry:
            continue
        name, tagger, body = entry.split("\0", 2)
        yield f"tag {name} (tagger)", tagger
        yield f"tag {name} (message)", body


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", action="store_true", help="also scan the history of HEAD and the tags")
    args = ap.parse_args(argv)
    found: List[str] = []
    for where, text in index_files():
        found += scan_lines(where, text)
    if args.history:
        for source in (history_blobs(), commits(), tags()):
            for where, text in source:
                found += scan_lines(where, text)
    for f in found:
        print(f)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
