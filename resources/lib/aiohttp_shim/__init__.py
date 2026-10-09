"""Minimal aiohttp stand-in for aiowebostv, backed by pure-Python websockets.

Only the surface aiowebostv 0.10.0 touches is implemented:

- ``ClientSession()`` with ``ws_connect(uri, heartbeat=, ssl=, max_msg_size=)``
  and ``close()``
- ``ClientWebSocketResponse`` with ``send_json``, ``send_str``,
  ``receive_json(timeout=)``, ``close`` and async iteration yielding
  ``WSMessage`` objects with ``.type`` and ``.data``
- ``WSMsgType``
- ``ClientConnectionError`` and ``WSServerHandshakeError``

Register it as ``sys.modules["aiohttp"]`` before importing aiowebostv
(see ``resources/lib/bootstrap.py``).
"""

from __future__ import annotations

import asyncio
import enum
import json
import ssl as ssl_module
from collections.abc import AsyncIterator, Callable
from typing import Any, NamedTuple

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    InvalidURI,
)

__all__ = [
    "ClientConnectionError",
    "ClientError",
    "ClientSession",
    "ClientWebSocketResponse",
    "WSMessage",
    "WSMsgType",
    "WSServerHandshakeError",
]


class ClientError(Exception):
    """Base class for shim client errors (mirrors aiohttp.ClientError)."""


class ClientConnectionError(ClientError):
    """Connection could not be established or was lost."""


class ClientConnectorError(ClientConnectionError, OSError):
    """TCP/TLS connect failure (mirrors aiohttp.ClientConnectorError)."""


class WSServerHandshakeError(ClientError):
    """The server rejected the WebSocket upgrade."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class WSMsgType(enum.IntEnum):
    """Subset of aiohttp.WSMsgType values (same numbers as aiohttp)."""

    CONTINUATION = 0x0
    TEXT = 0x1
    BINARY = 0x2
    PING = 0x9
    PONG = 0xA
    CLOSE = 0x8
    CLOSING = 0x100
    CLOSED = 0x101
    ERROR = 0x102


class WSMessage(NamedTuple):
    """A received WebSocket message."""

    type: WSMsgType
    data: Any
    extra: Any = None


_CLOSED_MESSAGE = WSMessage(WSMsgType.CLOSED, None)


def _ssl_context(ssl: Any) -> ssl_module.SSLContext | None:
    """Translate aiohttp's ``ssl`` argument into an SSLContext.

    aiohttp treats ``ssl=False`` as "TLS without verification" for https/wss.
    """
    if isinstance(ssl, ssl_module.SSLContext):
        return ssl
    if ssl is False:
        ctx = ssl_module.SSLContext(ssl_module.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl_module.CERT_NONE
        return ctx
    return ssl_module.create_default_context()


class ClientWebSocketResponse:
    """Wraps a websockets ClientConnection with aiohttp's API."""

    def __init__(self, conn: ClientConnection) -> None:
        self._conn = conn
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return True once the connection is closed."""
        return self._closed or self._conn.close_code is not None

    async def send_str(self, data: str) -> None:
        """Send a text frame."""
        try:
            await self._conn.send(data)
        except ConnectionClosed as err:
            raise ClientConnectionError(str(err)) from err

    async def send_json(
        self, data: Any, *, dumps: Callable[[Any], str] = json.dumps
    ) -> None:
        """Send a JSON-encoded text frame."""
        await self.send_str(dumps(data))

    async def receive(self, timeout: float | None = None) -> WSMessage:
        """Receive one message, mapping closure to a CLOSED message."""
        try:
            async with asyncio.timeout(timeout):
                data = await self._conn.recv()
        except ConnectionClosed:
            self._closed = True
            return _CLOSED_MESSAGE
        if isinstance(data, str):
            return WSMessage(WSMsgType.TEXT, data)
        return WSMessage(WSMsgType.BINARY, data)

    async def receive_str(self, *, timeout: float | None = None) -> str:
        """Receive a text message; raise TypeError for anything else."""
        msg = await self.receive(timeout)
        if msg.type is not WSMsgType.TEXT:
            error = f"Received message {msg.type}:{msg.data!r} is not str"
            raise TypeError(error)
        return msg.data

    async def receive_json(
        self,
        *,
        loads: Callable[[str], Any] = json.loads,
        timeout: float | None = None,
    ) -> Any:
        """Receive a text message and decode it as JSON."""
        return loads(await self.receive_str(timeout=timeout))

    async def close(self, *, code: int = 1000, message: bytes = b"") -> bool:
        """Close the connection. Returns False if it was already closed."""
        if self._closed:
            return False
        self._closed = True
        await self._conn.close(code=code, reason=message.decode("utf-8", "replace"))
        return True

    def __aiter__(self) -> AsyncIterator[WSMessage]:
        return self

    async def __anext__(self) -> WSMessage:
        msg = await self.receive()
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            raise StopAsyncIteration
        return msg


class ClientSession:
    """Factory for WebSocket connections, mirroring aiohttp.ClientSession."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._sockets: set[ClientWebSocketResponse] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return True once the session is closed."""
        return self._closed

    async def ws_connect(
        self,
        url: str,
        *,
        heartbeat: float | None = None,
        ssl: Any = True,
        max_msg_size: int = 4 * 1024 * 1024,
        **_kwargs: Any,
    ) -> ClientWebSocketResponse:
        """Open a WebSocket connection.

        Raises ClientConnectionError for transport failures and
        WSServerHandshakeError when the server rejects the upgrade, so
        aiowebostv's ws:3000 -> wss:3001 fallback works unchanged.
        """
        if self._closed:
            error = "Session is closed"
            raise RuntimeError(error)

        options: dict[str, Any] = {
            "max_size": max_msg_size or None,
            # aiohttp heartbeat: ping every N s, expect pong within N/2 s.
            "ping_interval": heartbeat,
            "ping_timeout": heartbeat / 2 if heartbeat else None,
            # aiowebostv wraps us in asyncio.timeout(); don't double up.
            "open_timeout": None,
            "compression": None,
            "proxy": None,
        }
        if url.startswith("wss://"):
            options["ssl"] = _ssl_context(ssl)

        try:
            conn = await connect(url, **options)
        except InvalidStatus as err:
            raise WSServerHandshakeError(
                str(err), status=err.response.status_code
            ) from err
        except InvalidURI as err:
            raise ClientConnectionError(str(err)) from err
        except InvalidHandshake as err:
            raise WSServerHandshakeError(str(err)) from err
        except (OSError, EOFError, ConnectionClosed) as err:
            raise ClientConnectorError(str(err)) from err

        ws = ClientWebSocketResponse(conn)
        self._sockets.add(ws)
        return ws

    async def close(self) -> None:
        """Close the session and any sockets it still owns."""
        if self._closed:
            return
        self._closed = True
        sockets, self._sockets = self._sockets, set()
        for ws in sockets:
            if not ws.closed:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass

    async def __aenter__(self) -> ClientSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
