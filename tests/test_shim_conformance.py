"""Run aiowebostv's real client against the fake TV through the aiohttp shim."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fake_tv import FakeTv

import aiohttp
from aiowebostv import WebOsClient, WebOsTvPairError

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "resources" / "lib" / "vendor"


async def _wait_for(predicate, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_aiohttp_is_the_shim():
    assert aiohttp.__name__ == "aiohttp_shim"
    assert sys.modules["aiohttp"] is aiohttp


def test_websockets_is_vendored_pure_python():
    import websockets
    import websockets.frames

    assert Path(websockets.__file__).is_relative_to(VENDOR)
    # The C speedups module must not be importable from the vendored tree.
    assert websockets.frames.apply_mask.__module__ == "websockets.utils"


def test_vendored_tree_has_no_native_code():
    native = [p for p in VENDOR.rglob("*") if p.suffix in {".so", ".pyd", ".dylib", ".c"}]
    assert native == []


@pytest.fixture
async def tv(patch_ports):
    tv = FakeTv()
    await tv.start()
    patch_ports(tv)
    yield tv
    await tv.stop()


async def test_connect_pair_subscribe_command_button_disconnect(tv):
    client = WebOsClient("127.0.0.1")
    updates = []

    async def on_state(state):
        updates.append(state.volume)

    await client.register_state_update_callback(on_state)
    assert await client.connect()
    assert client.client_key in tv.valid_keys
    assert tv.prompts == 1
    assert client.tv_state.volume == 10
    assert client.tv_state.current_app_id == "org.xbmc.kodi"
    assert "HDMI_1" not in client.tv_state.inputs  # keyed by appId
    assert "com.webos.app.hdmi1" in client.tv_state.inputs
    assert client.tv_state.is_on

    await client.set_volume(25)
    await _wait_for(lambda: client.tv_state.volume == 25)
    assert tv.model.volume == 25

    await tv.set_muted(True)
    await _wait_for(lambda: client.tv_state.muted is True)

    await client.button("HOME")
    await _wait_for(lambda: tv.buttons == ["HOME"])

    info = await client.request("system/getSystemInfo")
    assert info["modelName"] == "OLED65FAKE"

    await client.disconnect()
    assert not client.is_connected()
    assert 25 in updates


async def test_reconnect_with_stored_key_skips_prompt(tv):
    client = WebOsClient("127.0.0.1")
    await client.connect()
    key = client.client_key
    await client.disconnect()

    client2 = WebOsClient("127.0.0.1", client_key=key)
    assert await client2.connect()
    assert tv.prompts == 1
    await client2.disconnect()


async def test_pairing_rejected(patch_ports):
    tv = FakeTv(pairing="reject")
    await tv.start()
    patch_ports(tv)
    try:
        client = WebOsClient("127.0.0.1")
        with pytest.raises(WebOsTvPairError):
            await client.connect()
    finally:
        await tv.stop()


async def test_drop_ends_connection(tv):
    client = WebOsClient("127.0.0.1")
    await client.connect()
    await tv.drop_all()
    await _wait_for(lambda: not client.is_connected())
    assert client.tv_state.volume is None  # cleared on closeout


async def test_fallback_to_wss_when_ws_refused(tls_cert, patch_ports):
    tv = FakeTv(ws=False, wss_cert=tls_cert)
    await tv.start()
    patch_ports(tv)
    try:
        client = WebOsClient("127.0.0.1")
        assert await client.connect()
        await client.button("BACK")
        await _wait_for(lambda: tv.buttons == ["BACK"])
        await client.disconnect()
    finally:
        await tv.stop()


async def test_fallback_to_wss_when_ws_handshake_rejected(tls_cert, patch_ports):
    tv = FakeTv(reject_ws_handshake=True, wss_cert=tls_cert)
    await tv.start()
    patch_ports(tv)
    try:
        client = WebOsClient("127.0.0.1")
        assert await client.connect()
        await client.disconnect()
    finally:
        await tv.stop()


async def test_shim_raises_aiohttp_exception_types(patch_ports):
    from conftest import _closed_port

    session = aiohttp.ClientSession()
    with pytest.raises(aiohttp.ClientConnectionError):
        await session.ws_connect(f"ws://127.0.0.1:{_closed_port()}")

    tv = FakeTv(reject_ws_handshake=True)
    await tv.start()
    try:
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await session.ws_connect(f"ws://127.0.0.1:{tv.ws_port}")
    finally:
        await tv.stop()
        await session.close()


async def test_shim_message_iteration_and_receive_timeout():
    from websockets.asyncio.server import serve

    async def handler(conn):
        await conn.send("one")
        await conn.send(b"\x00")
        await conn.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(f"ws://127.0.0.1:{port}")
        msgs = [m async for m in ws]
        assert [m.type for m in msgs] == [aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY]
        assert msgs[0].data == "one"
        await session.close()

    async def silent(conn):
        await asyncio.sleep(1)

    async with serve(silent, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(f"ws://127.0.0.1:{port}")
        with pytest.raises(TimeoutError):
            await ws.receive_json(timeout=0.05)
        await session.close()


async def test_guard_blocks_non_loopback_connections():
    # conftest's autouse guard: tests must never reach a real TV.
    session = aiohttp.ClientSession()
    with pytest.raises(AssertionError, match="non-loopback"):
        await session.ws_connect("wss://192.0.2.1:3001")
    await session.close()


async def test_closed_at_hello_is_a_connection_error():
    # A TV in standby accepts the connection and closes it at the hello; that
    # once ended the service's connection loop (an unexpected TypeError).
    from websockets.exceptions import ConnectionClosedOK

    class Conn:
        close_code = None

        async def recv(self):
            raise ConnectionClosedOK(None, None)

    ws = aiohttp.ClientWebSocketResponse(Conn())
    with pytest.raises(aiohttp.ClientConnectionError):
        await ws.receive_json()
