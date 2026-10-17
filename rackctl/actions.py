"""Veiksmai su mašinomis: Wake-on-LAN paketas ir užmigdymas per SSH."""

import socket
import subprocess
from typing import Callable, List

from .decide import Machine


def magic_packet(mac: str) -> bytes:
    hexes = mac.replace(":", "").replace("-", "").lower()
    if len(hexes) != 12 or any(c not in "0123456789abcdef" for c in hexes):
        raise ValueError(f"netinkamas MAC: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(hexes) * 16


def wake(machine: Machine, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    packet = magic_packet(machine.mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, port))


# `rack-sleep` išėjimo kodai (machine/rack-sleep). Jis užmigdymą suplanuoja po kelių
# sekundžių ir išeina pirma, todėl SSH sesija baigiasi švariai, o ne nutrūksta.
SLEEP_OK = ("užmigs", "persikraus")
SLEEP_EXIT = {
    0: "užmigs po 5 s",
    3: "atsisakė: mašinoje vyksta job'as",
    4: "persikraus po 5 s: atnaujinimas prašė perkrovimo",
}


def ssh_sleep_command(machine: Machine, key_path: str, known_hosts: str) -> List[str]:
    # Mašinos authorized_keys šį raktą riboja viena komanda (`command=`), todėl komandos
    # čia neperduodame — ką paleisti, sprendžia mašina, ne valdiklis.
    return [
        "ssh",
        "-i", key_path,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        f"{machine.ssh_user}@{machine.host}",
    ]


def put_to_sleep(machine: Machine, key_path: str, known_hosts: str, run: Callable = subprocess.run) -> str:
    try:
        done = run(
            ssh_sleep_command(machine, key_path, known_hosts), capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        return "SSH neatsakė per 30 s"
    if done.returncode == 255:
        return f"SSH nepavyko: {done.stderr.strip()[:200]}"
    return SLEEP_EXIT.get(done.returncode, f"nežinomas kodas {done.returncode}")
