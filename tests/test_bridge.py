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
    assert kodi_side.actions == ["wheelup", "wheelup", "wheeldown"]


async def test_magic_remote_back_and_wheel_buttons(mapper, kodi_side):
    for code in (412, 1198, 1199):
        mapper.key(code, KEY_DOWN)
        mapper.key(code, KEY_UP)
    assert kodi_side.actions == ["back", "wheelup", "wheeldown"]


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
        rest = await asyncio.wait_for(reader.read(), 2.0)  # reads to EOF: the connection closed
        assert all(json.loads(line)["grab"] is False for line in rest.splitlines())
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


# pointer -------------------------------------------------------------------

class Mouse:
    def __init__(self) -> None:
        self.moves: list[tuple[int, int, int, int]] = []
        self.closed = False

    def move(self, x, y, max_x, max_y):
        self.moves.append((x, y, max_x, max_y))

    def ping(self):
        pass

    def close(self):
        self.closed = True


POINTER = dataclasses.replace(BASE, bridge_pointer=True)


async def read_msg(reader):
    return json.loads(await asyncio.wait_for(reader.readline(), 2.0))


async def connect_pointer(rb):
    reader, writer = await asyncio.open_connection("127.0.0.1", port_of(rb))
    hello = {"token": "secret-token", "version": "0.2.0", "pointer": [1920, 1080]}
    writer.write(json.dumps({"hello": hello}).encode() + b"\n")
    await writer.drain()
    assert await read_msg(reader) == {"ok": True}
    return reader, writer


async def test_pointer_takes_the_remote_over_only_while_kodi_is_on_screen(kodi_side):
    mouse = Mouse()
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: kodi_side.on_screen, None, mouse)
    await rb.start()
    try:
        reader, _writer = await connect_pointer(rb)
        grab = await read_msg(reader)
        assert grab["grab"] is True and grab["pointer"] is True
        assert {28, 103, 412, 1198, 272} <= set(grab["consume"])
        assert 115 not in grab["consume"]  # volume stays with the TV
        kodi_side.on_screen = False
        rb.refresh()
        assert (await read_msg(reader))["grab"] is False
        rb.refresh()  # no change, nothing sent
        kodi_side.on_screen = True
        rb.refresh()
        assert (await read_msg(reader))["grab"] is True
        rb.update(BASE)  # pointer turned off: hand the remote back
        assert (await read_msg(reader))["grab"] is False
    finally:
        await rb.close()
    assert mouse.closed


async def test_pointer_moves_kodis_mouse_and_click_selects(kodi_side):
    mouse = Mouse()
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: kodi_side.on_screen, None, mouse)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)
        for msg in ({"pos": [960, 540]}, {"key": 272, "value": 1}, {"key": 272, "value": 0}):
            writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: kodi_side.actions == ["select"])
        assert mouse.moves == [(960, 540, 1920, 1080)]
        kodi_side.on_screen = False
        writer.write(json.dumps({"pos": [10, 10]}).encode() + b"\n")
        await writer.drain()
        await asyncio.sleep(0.05)
        assert len(mouse.moves) == 1
    finally:
        await rb.close()


async def test_pointer_click_ignored_with_the_pointer_off(kodi_side):
    mouse = Mouse()
    rb = RemoteBridge(BASE, kodi_side.execute, lambda: True, None, mouse)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        assert (await read_msg(reader))["grab"] is False
        for msg in ({"pos": [1, 2]}, {"key": 272, "value": 1}, {"key": 272, "value": 0},
                    {"key": BACK, "value": 1}):
            writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: kodi_side.actions == ["back"])
        assert mouse.moves == []
    finally:
        await rb.close()


def test_eventserver_mouse_packets():
    import socket
    import struct

    from resources.lib.eventserver import HEADER, MS_ABSOLUTE, PT_HELO, PT_MOUSE, EventServerMouse

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2)
    mouse = EventServerMouse(server.getsockname()[1])
    try:
        mouse.move(1920, 540, 1920, 1080)
        packets = [server.recv(1024) for _ in range(2)]
    finally:
        mouse.close()
        server.close()
    headers = [HEADER.unpack(p[:32]) for p in packets]
    assert [h[0] for h in headers] == [b"XBMC", b"XBMC"]
    # Kodi's values (xbmc/network/EventPacket.h), not just our constants.
    assert (PT_HELO, PT_MOUSE) == (0x01, 0x04)
    assert [h[3] for h in headers] == [PT_HELO, PT_MOUSE]
    assert headers[0][7] == headers[1][7]  # same client uid
    assert packets[0][32:].startswith(b"LG Magic Remote\0")
    assert struct.unpack("!BHH", packets[1][32:]) == (MS_ABSOLUTE, 65535, 32768)


async def test_pointer_speed_scales_motion_and_new_strokes_dont_jump(kodi_side, monkeypatch):
    monkeypatch.setattr(bridge, "STROKE_GAP", 0.05)
    mouse = Mouse()
    rb = RemoteBridge(dataclasses.replace(POINTER, bridge_pointer_speed=2.0), kodi_side.execute,
                      lambda: True, None, mouse)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)

        async def pos(x, y):
            count = len(mouse.moves)
            writer.write(json.dumps({"pos": [x, y]}).encode() + b"\n")
            await writer.drain()
            await wait_for(lambda: len(mouse.moves) == count + 1)
            return mouse.moves[-1][:2]

        assert await pos(900, 500) == (900, 500)   # first position: where the TV puts it
        assert await pos(910, 505) == (920, 510)   # motion doubled
        assert await pos(1900, 505) == (1920, 510)  # clamped to the screen
        await asyncio.sleep(0.1)                    # pause: a new stroke
        assert await pos(300, 300) == (1920, 510)   # the TV's jump doesn't move the cursor
        assert await pos(290, 300) == (1900, 510)
        rb.update(dataclasses.replace(POINTER, bridge_pointer_speed=1.0))
        assert await pos(100, 200) == (100, 200)   # 100%: exactly the TV's position
    finally:
        await rb.close()


async def test_tvs_cursor_images_are_handed_to_kodi(kodi_side):
    import base64

    got: list[tuple[dict[str, bytes], str]] = []
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, Mouse(),
                      lambda images, hotspot: got.append((images, hotspot)))
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 20000  # bigger than one old-style line
        msg = {"cursor": {"point": base64.b64encode(png).decode(), "hover": "not base64!"},
               "hotspot": "corner"}
        writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: got)
        assert got == [({"point": png}, "corner")]
    finally:
        await rb.close()


async def test_pointer_stays_hidden_after_a_button_until_the_remote_really_moves(kodi_side):
    mouse = Mouse()
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, mouse)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)

        async def send(*msgs):
            for msg in msgs:
                writer.write(json.dumps(msg).encode() + b"\n")
            await writer.drain()
            await asyncio.sleep(0.05)

        await send({"pos": [900, 500]})
        await send({"key": 108, "value": 1}, {"key": 108, "value": 0})  # Down
        await send({"pos": [905, 503]}, {"pos": [890, 520]})           # hand jitter
        assert [m[:2] for m in mouse.moves] == [(900, 500)]
        await send({"pos": [1100, 600]})                                # a real move
        assert [m[:2] for m in mouse.moves] == [(900, 500), (1100, 600)]
        await send({"key": 272, "value": 1}, {"key": 272, "value": 0}, {"pos": [1102, 601]})
        assert mouse.moves[-1][:2] == (1102, 601)  # a click keeps the pointer
        await send({"wheel": -1}, {"pos": [1110, 605]})
        assert mouse.moves[-1][:2] == (1102, 601)  # so does nothing after the wheel
    finally:
        await rb.close()


@pytest.mark.parametrize(("container", "skin", "expect"), [
    ("12", "", "trigger"),                       # vertical list: analog scroll
    ("12", "WebOS.WheelSideways", "right"),      # a row: one item sideways
    ("12", "WebOS.WheelButtons", "down"),        # a grid panel: one row down
    ("", "", "down"),                            # not a list
])
def test_wheel_scrolls_lists_along_their_direction(kodi, container, skin, expect):
    from resources.lib.service import SCROLL_AMOUNT, WebOsService

    service = WebOsService()
    service.mouse = Mouse()
    service.mouse.analog = lambda button, amount: service.mouse.moves.append((button, amount))
    kodi.info_labels["System.CurrentControlID"] = "500"
    kodi.info_labels["Container(500).NumItems"] = container
    if skin:
        kodi.info_labels[f"Window(Home).Property({skin})"] = "Control.HasFocus(500)"
        kodi.conditions["Control.HasFocus(500)"] = True
    service._remote_button("wheeldown")
    if expect == "trigger":
        assert service.mouse.moves == [("rightanalogtrigger", SCROLL_AMOUNT)]
        assert round(255 * SCROLL_AMOUNT) ** 2 / 255**2 > 0.4  # one item per notch
    else:
        assert service.mouse.moves == []
        assert kodi.jsonrpc_calls[-1]["params"] == {"button": expect, "keymap": "R1"}


def test_eventserver_analog_button_packet():
    import socket
    import struct

    from resources.lib.eventserver import HEADER, PT_BUTTON, EventServerMouse

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2)
    es = EventServerMouse(server.getsockname()[1])
    try:
        es.analog("leftanalogtrigger", 0.5)
        server.recv(1024)  # HELO
        packet = server.recv(1024)
    finally:
        es.close()
        server.close()
    assert PT_BUTTON == 0x03  # Kodi's value
    assert HEADER.unpack(packet[:32])[3] == PT_BUTTON
    code, flags, amount = struct.unpack("!HHH", packet[32:38])
    assert (code, flags, amount) == (0, 0x01 | 0x02 | 0x08 | 0x10 | 0x20, 32768)
    assert packet[38:] == b"XG\0leftanalogtrigger\0"
