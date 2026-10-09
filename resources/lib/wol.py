"""Wake-on-LAN magic packet (only meaningful when Kodi runs off the TV)."""

from __future__ import annotations

import re
import socket

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([-:]?)(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}$")


def parse_mac(mac: str) -> bytes:
    mac = mac.strip()
    if not _MAC_RE.match(mac):
        error = f"Invalid MAC address: {mac!r}"
        raise ValueError(error)
    return bytes.fromhex(re.sub(r"[-:]", "", mac))


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + parse_mac(mac) * 16


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))
