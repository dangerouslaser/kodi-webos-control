"""Find LG webOS TVs on the LAN with SSDP (stdlib only).

webOS TVs answer an M-SEARCH for the second-screen service with a USN
carrying a stable device UUID, the same value the SSAP hello reports as
``deviceUUID``. That UUID is how we recognise a paired TV at a new IP.
"""

from __future__ import annotations

import asyncio
import re
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from . import storage

ST = "urn:lge-com:service:webos-second-screen:1"
SSDP_TARGET = ("239.255.255.250", 1900)
HINTS_FILE = "discovery.json"
_UUID_RE = re.compile(r"uuid:([0-9A-Fa-f-]+)")


@dataclass(frozen=True)
class DiscoveredTv:
    host: str
    uuid: str
    name: str = "LG webOS TV"
    model: str = ""

    @property
    def label(self) -> str:
        model = f" ({self.model})" if self.model and self.model not in self.name else ""
        return f"{self.name}{model} - {self.host}"


def _search_message(mx: int) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_TARGET[0]}:{SSDP_TARGET[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {ST}\r\n\r\n"
    ).encode()


def parse_response(data: bytes) -> dict[str, str] | None:
    """Parse an SSDP response into lower-cased headers, or None."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    lines = text.split("\r\n")
    if not lines or not lines[0].upper().startswith("HTTP/1.1 200"):
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return headers


def is_webos(headers: dict[str, str]) -> bool:
    # Some devices (e.g. Hue bridges) answer every search; check ST/USN.
    return headers.get("st", "").lower() == ST or ST in headers.get("usn", "").lower()


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.responses: dict[str, dict[str, str]] = {}

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        headers = parse_response(data)
        if headers and is_webos(headers):
            match = _UUID_RE.search(headers.get("usn", ""))
            if match:
                headers["uuid"] = match.group(1).lower()
                self.responses.setdefault(addr[0], headers)


def _fetch_description(location: str, timeout: float) -> tuple[str, str]:
    with urllib.request.urlopen(location, timeout=timeout) as resp:  # noqa: S310
        xml = resp.read(65536).decode("utf-8", "replace")
    name = re.search(r"<friendlyName>(.*?)</friendlyName>", xml, re.S)
    model = re.search(r"<modelNumber>(.*?)</modelNumber>", xml, re.S)
    return (name.group(1).strip() if name else "", model.group(1).strip() if model else "")


async def discover(
    timeout: float = 3.0,
    *,
    target: tuple[str, int] = SSDP_TARGET,
    fetch_description: bool = True,
) -> list[DiscoveredTv]:
    """Search the LAN for webOS TVs. Never raises for network errors."""
    loop = asyncio.get_running_loop()
    try:
        transport, proto = await loop.create_datagram_endpoint(
            _Collector, local_addr=("0.0.0.0", 0)
        )
    except OSError:
        return []
    try:
        message = _search_message(max(1, int(timeout) - 1))
        for _ in range(2):
            try:
                transport.sendto(message, target)
            except OSError:
                pass
            await asyncio.sleep(min(0.3, timeout / 4))
        await asyncio.sleep(max(0.0, timeout - 0.6))
    finally:
        transport.close()

    async def build(host: str, headers: dict[str, str]) -> DiscoveredTv:
        name, model = "", ""
        location = headers.get("location", "")
        # Only fetch descriptions served by the responder itself.
        if fetch_description and location and urlparse(location).hostname == host:
            try:
                name, model = await loop.run_in_executor(
                    None, _fetch_description, location, 2.0
                )
            except Exception:  # noqa: BLE001
                pass
        return DiscoveredTv(host=host, uuid=headers["uuid"], name=name or "LG webOS TV", model=model)

    found = await asyncio.gather(*(build(h, hd) for h, hd in proto.responses.items()))
    return sorted(found, key=lambda tv: (tv.name.casefold(), tv.host))


def remember(found: list[DiscoveredTv]) -> None:
    """Record host -> UUID hints so a pick or re-discovery reuses the key."""
    hints = storage.read_json(HINTS_FILE, {}) or {}
    hosts = hints.get("hosts") if isinstance(hints.get("hosts"), dict) else {}
    for tv in found:
        hosts[tv.host] = tv.uuid
    storage.write_json(HINTS_FILE, {"hosts": hosts})


def hinted_uuid(host: str) -> str | None:
    hints = storage.read_json(HINTS_FILE, {}) or {}
    hosts = hints.get("hosts") if isinstance(hints, dict) else None
    value = hosts.get(host) if isinstance(hosts, dict) else None
    return value if isinstance(value, str) else None
