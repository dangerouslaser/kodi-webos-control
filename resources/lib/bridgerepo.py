"""The Remote Bridge TV app, served to the TV's Homebrew Channel from Kodi.

The add-on carries the app it was built and tested with (resources/bridge/,
from scripts/bundle_bridge.sh) and serves it as a one-package Homebrew
Channel repository, so the TV installs and updates exactly that version from
this box: nothing to publish, no internet needed.

    GET /repo.json        the repository index (its URLs use the address the
                          TV reached us on)
    GET /<ipk>            the app
    GET /icon.png         its icon
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

from . import kodilog as log

BUNDLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")
SOURCE_URL = "https://github.com/dangerouslaser/kodi-remote-bridge"
MAX_REQUEST = 8192
TIMEOUT = 10.0


def bundled(directory: str = BUNDLE_DIR) -> dict[str, Any] | None:
    """The bundled app's details (bridge.json), or None if it's missing."""
    try:
        with open(os.path.join(directory, "bridge.json"), encoding="utf-8") as f:
            info = json.load(f)
        if os.path.isfile(os.path.join(directory, info["ipk"])):
            return info
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def version_tuple(version: str) -> tuple[int, ...]:
    parts = []
    for part in str(version).split("."):
        digits = "".join(c for c in part if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def repo_index(info: dict[str, Any], base: str) -> dict[str, Any]:
    """The Homebrew Channel repository index for the bundled app at `base` (http://host:port)."""
    icon = f"{base}/icon.png"
    manifest = {
        "id": info["id"], "version": info["version"], "type": "web",
        "title": info["title"], "appDescription": info["appDescription"],
        "iconUri": icon, "sourceUrl": SOURCE_URL, "rootRequired": True,
        "ipkUrl": f"{base}/{info['ipk']}", "ipkSize": info["size"],
        "ipkHash": {"sha256": info["sha256"]},
    }
    return {
        "paging": {"page": 1, "count": 1, "maxPage": 1, "itemsTotal": 1},
        "packages": [{"id": info["id"], "title": info["title"], "iconUri": icon, "manifest": manifest}],
    }


class RepoServer:
    """A minimal HTTP server for the three files (loop thread)."""

    def __init__(self, directory: str = BUNDLE_DIR) -> None:
        self._dir = directory
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self, port: int) -> None:
        if self._server is not None and port == self.port:
            return
        await self.close()
        if bundled(self._dir) is None:
            log.warning("remote bridge: no bundled TV app to serve")
            return
        try:
            self._server = await asyncio.start_server(self._client, host="0.0.0.0", port=port)
        except OSError as err:
            log.warning(f"remote bridge: cannot serve the TV app on port {port}: {err}")
            return
        self.port = port

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 1.0)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
            if len(head) > MAX_REQUEST:
                return
            method, path = (head.split(b"\r\n", 1)[0].decode("latin-1").split(" ") + ["", ""])[:2]
            host = (writer.get_extra_info("sockname") or ("127.0.0.1",))[0]
            status, kind, body = self._respond(method, path.split("?", 1)[0], host)
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n"
                "Cache-Control: no-cache\r\nConnection: close\r\n\r\n".encode() + body
            )
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
                UnicodeDecodeError):
            pass
        finally:
            writer.close()

    def _respond(self, method: str, path: str, host: str) -> tuple[str, str, bytes]:
        info = bundled(self._dir)
        if method != "GET" or info is None:
            return "404 Not Found", "text/plain", b"not found"
        if path == "/repo.json":
            body = json.dumps(repo_index(info, f"http://{host}:{self.port}")).encode()
            return "200 OK", "application/json", body
        files = {f"/{info['ipk']}": (info["ipk"], "application/vnd.debian.binary-package"),
                 "/icon.png": ("icon.png", "image/png")}
        if path not in files:
            return "404 Not Found", "text/plain", b"not found"
        name, kind = files[path]
        try:
            with open(os.path.join(self._dir, name), "rb") as f:
                return "200 OK", kind, f.read()
        except OSError:
            return "404 Not Found", "text/plain", b"not found"
