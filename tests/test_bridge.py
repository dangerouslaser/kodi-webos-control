"""Remote Bridge: TV remote presses forwarded by the rooted-TV app become Kodi actions."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from resources.lib import bridge
from resources.lib.bridge import KEY_DOWN, KEY_REPEAT, KEY_UP, KeyMapper, RemoteBridge
from resources.lib.settings import Settings

OK, UP, BACK, PLAY, UNKNOWN = 28, 103, 158, 207, 999


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch):
    monkeypatch.setattr(bridge, "LONG_PRESS", 0.15)
    monkeypatch.setattr(bridge, "REPEAT_MIN_INTERVAL", 0.03)


class Kodi:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.on_screen = True

    def execute(self, action: str) -> None:
        self.actions.append(action)


@pytest.fixture
def kodi_side():
    return Kodi()


@pytest.fixture
def mapper(kodi_side):
    return KeyMapper(kodi_side.execute, lambda: kodi_side.on_screen)


async def test_press_runs_the_mapped_action(mapper, kodi_side):
    mapper.key(BACK, KEY_DOWN)
    mapper.key(BACK, KEY_UP)
    mapper.key(PLAY, KEY_DOWN)
    assert kodi_side.actions == ["back", "play"]


async def test_ignored_while_kodi_is_not_on_screen(mapper, kodi_side):
    kodi_side.on_screen = False
    for code in (BACK, UP, OK):
        mapper.key(code, KEY_DOWN)
        mapper.key(code, KEY_UP)
    mapper.wheel(1)
    await asyncio.sleep(0.25)
    assert kodi_side.actions == []


async def test_short_ok_selects_on_release(mapper, kodi_side):
    mapper.key(OK, KEY_DOWN)
    assert kodi_side.actions == []
    mapper.key(OK, KEY_UP)
    assert kodi_side.actions == ["select"]


async def test_held_ok_opens_the_context_menu_once(mapper, kodi_side):
    mapper.key(OK, KEY_DOWN)
    await asyncio.sleep(0.25)
    mapper.key(OK, KEY_REPEAT)
    mapper.key(OK, KEY_UP)
    assert kodi_side.actions == ["menu"]


async def test_held_arrow_repeats_on_the_tvs_repeats_throttled(mapper, kodi_side):
    mapper.key(UP, KEY_DOWN)
    mapper.key(UP, KEY_REPEAT)  # too soon after the press: dropped
    await asyncio.sleep(0.05)
    mapper.key(UP, KEY_REPEAT)
    mapper.key(UP, KEY_UP)
    mapper.key(UP, KEY_REPEAT)  # after release: ignored
    assert kodi_side.actions == ["up", "up"]


async def test_nothing_repeats_without_the_tvs_repeats(mapper, kodi_side):
    # A press whose release never arrives must not keep going on its own.
    mapper.key(UP, KEY_DOWN)
    await asyncio.sleep(0.2)
    assert kodi_side.actions == ["up"]


async def test_repeats_stop_when_kodi_leaves_the_screen(mapper, kodi_side):
    mapper.key(UP, KEY_DOWN)
    kodi_side.on_screen = False
    await asyncio.sleep(0.05)
    mapper.key(UP, KEY_REPEAT)
    assert kodi_side.actions == ["up"]


async def test_only_arrows_repeat(mapper, kodi_side):
    mapper.key(BACK, KEY_DOWN)
    await asyncio.sleep(0.05)
    mapper.key(BACK, KEY_REPEAT)
    assert kodi_side.actions == ["back"]


async def test_unmapped_button_is_logged_once(mapper, kodi_side, kodi):
    mapper.key(UNKNOWN, KEY_DOWN)
    mapper.key(UNKNOWN, KEY_UP)
    mapper.key(UNKNOWN, KEY_DOWN)
    assert kodi_side.actions == []
    assert sum("button 999 has no Kodi button" in m for _, m in kodi.logs) == 1


async def test_wheel_scrolls(mapper, kodi_side):
    mapper.wheel(2)
    mapper.wheel(-1)
    assert kodi_side.actions == ["up", "up", "down"]


async def test_magic_remote_back_and_wheel_buttons(mapper, kodi_side):
    for code in (412, 1198, 1199):
        mapper.key(code, KEY_DOWN)
        mapper.key(code, KEY_UP)
    assert kodi_side.actions == ["back", "up", "down"]


# server -------------------------------------------------------------------

BASE = Settings(remote_bridge=True, bridge_port=0, bridge_token="secret-token")


async def start_bridge(kodi_side, settings=BASE):
    rb = RemoteBridge(settings, kodi_side.execute, lambda: kodi_side.on_screen)
    await rb.start()
    return rb


def port_of(rb: RemoteBridge) -> int:
    assert rb._server is not None
    return rb._server.sockets[0].getsockname()[1]


async def connect(rb, token="secret-token"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port_of(rb))
    writer.write(json.dumps({"hello": {"token": token, "version": "0.1.0"}}).encode() + b"\n")
    await writer.drain()
    return reader, writer


async def wait_for(predicate, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_tv_with_the_token_drives_kodi(kodi_side):
    rb = await start_bridge(kodi_side)
    try:
        reader, writer = await connect(rb)
        assert json.loads(await reader.readline()) == {"ok": True}
        for value in (KEY_DOWN, KEY_UP):
            writer.write(json.dumps({"key": BACK, "value": value, "dev": 0}).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: kodi_side.actions == ["back"])
        status = rb.status()
        assert status["remote_bridge_connected"] is True
        assert status["remote_bridge_version"] == "0.1.0"
        writer.close()
        await wait_for(lambda: not rb.status()["remote_bridge_connected"])
    finally:
        await rb.close()


async def test_wrong_token_is_rejected(kodi_side, kodi):
    rb = await start_bridge(kodi_side)
    try:
        reader, writer = await connect(rb, token="guess")
        assert await reader.readline() == b""
        writer.write(json.dumps({"key": BACK, "value": KEY_DOWN}).encode() + b"\n")
        await asyncio.sleep(0.05)
        assert kodi_side.actions == []
        assert rb.status()["remote_bridge_connected"] is False
        assert any("rejected a connection" in m for _, m in kodi.logs)
        writer.close()
    finally:
        await rb.close()


async def test_not_listening_when_off_or_without_token(kodi_side):
    for settings in (dataclasses.replace(BASE, remote_bridge=False),
                     dataclasses.replace(BASE, bridge_token="")):
        rb = await start_bridge(kodi_side, settings)
        assert rb.status()["remote_bridge_listening"] is False
        await rb.close()


async def test_turning_it_off_closes_the_tv_connection(kodi_side):
    rb = await start_bridge(kodi_side)
    try:
        reader, _writer = await connect(rb)
        await reader.readline()
        rb.update(dataclasses.replace(BASE, remote_bridge=False))
        assert await asyncio.wait_for(reader.read(), 2.0) == b""
        assert rb.status()["remote_bridge_listening"] is False
    finally:
        await rb.close()


async def test_state_changes_are_published_for_skins(kodi_side):
    states: list[str] = []
    rb = RemoteBridge(BASE, kodi_side.execute, lambda: True, states.append)
    await rb.start()
    try:
        reader, writer = await connect(rb)
        await reader.readline()
        await wait_for(lambda: states[-1:] == ["connected"])
        writer.close()
        await wait_for(lambda: states[-1:] == ["waiting"])
        rb.update(dataclasses.replace(BASE, remote_bridge=False))
        await wait_for(lambda: states[-1:] == [""])
    finally:
        await rb.close()
    assert states == ["waiting", "connected", "waiting", ""]


def test_buttons_go_through_kodis_remote_keymap(kodi):
    from resources.lib.service import _press_button

    _press_button("select")
    assert kodi.jsonrpc_calls[-1]["method"] == "Input.ButtonEvent"
    assert kodi.jsonrpc_calls[-1]["params"] == {"button": "select", "keymap": "R1"}
