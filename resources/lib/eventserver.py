"""Minimal Kodi EventServer client: moves Kodi's mouse pointer, and sends
analog gamepad buttons.

JSON-RPC can't move the mouse or send an analog amount; the EventServer
(UDP, services.esport, enabled by default) can. Packets follow Kodi's EventServer protocol 2.0
(xbmc/network/EventPacket.h): a 32-byte header, then the payload.
"""

from __future__ import annotations

import random
import socket
import struct
import time

HEADER = struct.Struct("!4sBBHIIHI10s")
# Packet types from xbmc/network/EventPacket.h.
PT_HELO = 0x01
PT_BUTTON = 0x03
PT_MOUSE = 0x04
PT_PING = 0x05
MS_ABSOLUTE = 0x01
# Button packet flags.
PTB_USE_NAME, PTB_DOWN, PTB_USE_AMOUNT, PTB_QUEUE, PTB_NO_REPEAT = 0x01, 0x02, 0x08, 0x10, 0x20
ICON_NONE = 0x00
# Kodi forgets a client after 60 s without packets.
PING_INTERVAL = 30.0
DEFAULT_PORT = 9777


class EventServerMouse:
    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                 name: str = "LG Magic Remote") -> None:
        self._addr = (host, port)
        self._name = name
        self._uid = random.randint(1, 0x7FFFFFFF)
        self._sock: socket.socket | None = None
        self._last_packet = 0.0

    def move(self, x: int, y: int, max_x: int, max_y: int) -> None:
        """Absolute position in the remote's range (0..max) -> Kodi's 0..65535."""
        sx = _scale(x, max_x)
        sy = _scale(y, max_y)
        self._send(PT_MOUSE, struct.pack("!BHH", MS_ABSOLUTE, sx, sy))

    def analog(self, button: str, amount: float, keymap: str = "XG") -> None:
        """One press of an analog button (0..1), e.g. a gamepad trigger."""
        flags = PTB_USE_NAME | PTB_DOWN | PTB_USE_AMOUNT | PTB_QUEUE | PTB_NO_REPEAT
        value = max(0, min(65535, round(amount * 65535)))
        payload = struct.pack("!HHH", 0, flags, value) + keymap.encode() + b"\0" + button.encode() + b"\0"
        self._send(PT_BUTTON, payload)

    def ping(self) -> None:
        if time.monotonic() - self._last_packet >= PING_INTERVAL:
            self._send(PT_PING, b"")

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _send(self, packet_type: int, payload: bytes) -> None:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._packet(PT_HELO, self._name.encode() + b"\0" + struct.pack("!BHII", ICON_NONE, 0, 0, 0))
        self._packet(packet_type, payload)

    def _packet(self, packet_type: int, payload: bytes) -> None:
        header = HEADER.pack(b"XBMC", 2, 0, packet_type, 1, 1, len(payload), self._uid, b"\0" * 10)
        try:
            assert self._sock is not None
            self._sock.sendto(header + payload, self._addr)
            self._last_packet = time.monotonic()
        except OSError:
            self.close()


def _scale(value: int, maximum: int) -> int:
    if maximum <= 0:
        return 0
    return max(0, min(65535, round(value * 65535 / maximum)))
