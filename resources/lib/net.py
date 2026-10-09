"""Small network helpers: LAN IP discovery and backoff."""

from __future__ import annotations

import socket


def discover_lan_ip() -> str | None:
    """Return this host's primary LAN IP using the UDP connect trick.

    Connecting a UDP socket sends nothing; it only makes the kernel pick
    the outbound interface, whose address we then read back.
    """
    for target in ("10.255.255.255", "192.0.2.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 1))
                ip = sock.getsockname()[0]
        except OSError:
            continue
        if ip and not ip.startswith("127.") and ip != "0.0.0.0":
            return ip
    return None


class Backoff:
    """Exponential backoff: base, 2*base, 4*base ... capped."""

    def __init__(self, base: float = 2.0, cap: float = 60.0) -> None:
        self.base = base
        self.cap = cap
        self._attempt = 0

    def next(self) -> float:
        delay = min(self.cap, self.base * (2**self._attempt))
        if delay < self.cap:
            self._attempt += 1
        return delay

    def reset(self) -> None:
        self._attempt = 0
