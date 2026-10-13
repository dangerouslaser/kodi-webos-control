"""Remote Bridge: TV remote presses forwarded by the rooted-TV app become Kodi actions."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import types

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


async def test_magic_remote_back_but_not_its_pointer_signals(mapper, kodi_side):
    for code in (412, 1198, 1199):  # Back, pointer waking, pointer idle
        mapper.key(code, KEY_DOWN)
        mapper.key(code, KEY_UP)
    assert kodi_side.actions == ["back"]


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
    rb._bundled = None  # no newer app to offer
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
        assert {28, 103, 412, 272} <= set(grab["consume"])
        assert not {1198, 1199} & set(grab["consume"])  # the TV's pointer signals
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


async def test_pointer_follows_the_tv_one_to_one(kodi_side):
    mouse = Mouse()
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, mouse)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)
        for x, y in ((900, 500), (1919, 0), (0, 1079)):
            writer.write(json.dumps({"pos": [x, y]}).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: len(mouse.moves) == 3)
        assert [m[:2] for m in mouse.moves] == [(900, 500), (1919, 0), (0, 1079)]
    finally:
        await rb.close()


async def read_until(reader, key):
    while True:
        msg = await read_msg(reader)
        if key in msg:
            return msg


async def test_tv_pointer_settings_come_from_the_tv_and_changes_go_back(kodi_side):
    reported: list[dict[str, str]] = []
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, Mouse(), None, None,
                      reported.append)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        tv = {"pointerSpeed": "normal", "pointerSize": "medium", "pointerShape": "auto",
              "pointerAlignment": "off", "cursorAutoRemover": "on"}
        writer.write(json.dumps({"tv": tv}).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: reported)
        assert reported == [tv]
        # Kodi's settings now mirror the TV: nothing to send.
        mirrored = dataclasses.replace(POINTER, tv_pointer=tuple(tv.items()))
        rb.update(mirrored)
        # The user picks Fast and a large pointer here: sent to the TV.
        changed = {**tv, "pointerSpeed": "fast", "pointerSize": "large"}
        rb.update(dataclasses.replace(POINTER, tv_pointer=tuple(changed.items())))
        msg = await read_until(reader, "set_tv")
        assert msg == {"set_tv": {"pointerSpeed": "fast", "pointerSize": "large"}}
    finally:
        await rb.close()


def test_tv_pointer_settings_map_to_addon_values():
    from resources.lib.settings import tv_pointer_to_addon

    assert tv_pointer_to_addon({"pointerSpeed": "fast", "pointerShape": "c", "pointerAlignment": "on",
                                "cursorAutoRemover": "off", "pointerSize": "huge", "other": "x"}) == {
        "tv_pointer_speed": "2", "tv_pointer_shape": "3", "tv_pointer_align": "true",
        "tv_pointer_autohide": "false"}


async def test_pointer_stays_up_with_the_tvs_auto_hide_off(kodi_side):
    mouse = Mouse()
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, mouse)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)
        writer.write(json.dumps({"pos": [900, 500]}).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: mouse.moves)
        info = next(iter(rb._clients.values()))
        rb._keep_pointer(info)
        assert len(mouse.moves) == 1  # auto-hide on (the default): Kodi hides it
        info["tv"] = {"cursorAutoRemover": "off"}
        rb._keep_pointer(info)
        rb._keep_pointer(info)
        assert [m[:2] for m in mouse.moves[1:]] == [(901, 500), (900, 500)]
        info["hidden_at"] = (900, 500)  # a button hid it: stays hidden
        rb._keep_pointer(info)
        assert len(mouse.moves) == 3
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


class Wheel:
    def __init__(self, works=True):
        self.works = works
        self.scrolls = []
        self.closed = False

    def wheel(self, x, y, notches):
        self.scrolls.append((round(x, 3), round(y, 3), notches))
        return self.works

    def close(self):
        self.closed = True


@pytest.mark.parametrize("works", [True, False])
async def test_wheel_while_pointing_scrolls_under_the_pointer_and_keeps_it(kodi_side, works):
    mouse, wheel = Mouse(), Wheel(works)
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, mouse, wheel=wheel)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)

        async def send(*msgs):
            for msg in msgs:
                writer.write(json.dumps(msg).encode() + b"\n")
            await writer.drain()
            await asyncio.sleep(0.05)

        await send({"pos": [960, 270]}, {"wheel": -1}, {"wheel": 2})
        # Once the virtual wheel fails, the pointer is down and it isn't tried again.
        assert wheel.scrolls == [(0.5, 0.25, -1), (0.5, 0.25, 2)][:2 if works else 1]
        await send({"pos": [965, 272]})  # a small move right after
        if works:
            assert kodi_side.actions == []  # not the focused list
            assert mouse.moves[-1][:2] == (965, 272)  # the pointer stayed up
        else:  # no virtual wheel: the wheel moves the focus and hides the pointer
            assert kodi_side.actions == ["wheeldown", "wheelup", "wheelup"]
            assert mouse.moves[-1][:2] == (960, 270)
    finally:
        await rb.close()
    assert wheel.closed


async def test_wheel_in_the_guide_goes_to_the_add_on(kodi_side):
    mouse, wheel, focus = Mouse(), Wheel(), Focus(guide=True)
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, mouse, focus=focus, wheel=wheel)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)
        writer.write(b'{"pos": [960, 500]}\n{"wheel": -1}\n{"pos": [962, 501]}\n')
        await writer.drain()
        await wait_for(lambda: len(mouse.moves) == 2)
        assert "wheel -1" in focus.calls
        assert wheel.scrolls == [] and kodi_side.actions == []
        assert mouse.moves[-1][:2] == (962, 501)  # the pointer carries on
    finally:
        await rb.close()


async def test_wheel_without_the_pointer_moves_the_focus(kodi_side):
    wheel = Wheel()
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, Mouse(), wheel=wheel)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)
        writer.write(b'{"wheel": -1}\n')
        await writer.drain()
        await wait_for(lambda: kodi_side.actions)
        assert kodi_side.actions == ["wheeldown"] and wheel.scrolls == []
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


class Focus:
    def __init__(self, where=None, guide=False) -> None:
        self.calls: list[str] = []
        self.where = where
        self.guide = guide  # the wheel moves the highlight (the TV guide)

    def track(self, x=-1, y=-1):
        self.calls.append("track")

    def restore(self):
        self.calls.append("restore")

    def key(self):
        self.calls.append("key")

    def click(self):
        self.calls.append("click")

    def spot(self):
        return self.where

    def wheel(self, items):
        self.calls.append(f"wheel {items}")
        return self.guide


async def test_resting_pointer_refocuses_once_and_before_buttons(kodi_side, monkeypatch):
    monkeypatch.setattr(bridge, "POINTER_REST", 0.05)
    monkeypatch.setattr(bridge, "FOCUS_TRACK", 0.0)
    focus = Focus()
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: kodi_side.on_screen, None, Mouse(),
                      None, focus)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)

        async def send(*msgs, wait=0.02):
            for msg in msgs:
                writer.write(json.dumps(msg).encode() + b"\n")
            await writer.drain()
            await asyncio.sleep(wait)

        def restores():
            return focus.calls.count("restore")

        await send({"pos": [100, 100]}, {"pos": [120, 100]})
        assert focus.calls == ["track", "track"]   # noted while moving
        await asyncio.sleep(0.1)
        assert restores() == 1                     # came to rest: once
        await asyncio.sleep(0.1)
        assert restores() == 1
        await send({"pos": [300, 300]}, {"key": 108, "value": 1}, {"key": 108, "value": 0})
        assert restores() == 2                     # a button right after moving: first
        assert kodi_side.actions == ["down"]
        await asyncio.sleep(0.1)
        assert restores() == 2                     # not again when the timer would have fired
    finally:
        await rb.close()


@pytest.mark.parametrize(("item", "builtin"), [("4", "SetFocus(5100,3,absolute)"), ("", "SetFocus(5100)")])
async def test_focus_goes_back_to_the_last_item_the_pointer_was_over(kodi, item, builtin):
    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    kodi.info_labels.update({"System.CurrentWindow": "Home", "System.CurrentControlID": "5100",
                             "Container(5100).CurrentItem": item})
    keeper.track()
    keeper.restore()
    assert kodi.builtins == []  # still focused: nothing to do
    kodi.info_labels["System.CurrentControlID"] = ""  # pointer over empty space
    keeper.track()  # remembers the last control, not "nothing"
    keeper.restore()
    assert kodi.builtins == [builtin]
    kodi.info_labels["System.CurrentWindow"] = "Videos"  # another window: leave it alone
    keeper.restore()
    assert kodi.builtins == [builtin]
    keeper.close()


async def test_bridge_tells_the_focus_keeper_about_buttons_and_clicks(kodi_side):
    focus = Focus()
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, Mouse(), None, focus)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)
        for msg in ({"key": 272, "value": 1}, {"key": 272, "value": 0},
                    {"key": 108, "value": 1}, {"key": 108, "value": 0}, {"wheel": 1}):
            writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: focus.calls == ["click", "key", "key"])
        assert kodi_side.actions == ["select", "down", "wheelup"]
    finally:
        await rb.close()


@pytest.fixture
def home(kodi, monkeypatch):
    """Estuary's home screen, side menu on Movies (item 2) with focus in the widgets."""
    import xbmcgui

    from resources.lib import service as service_module

    monkeypatch.setattr(service_module, "POINTER_IDLE", 0.05)
    kodi.conditions["Window.IsActive(home)"] = True
    kodi.info_labels.update({"Container(9000).ListItem.Property(id)": "movies",
                             "Container(9000).CurrentItem": "2"})

    def hover(section, item):
        kodi.info_labels.update({"Container(9000).ListItem.Property(id)": section,
                                 "Container(9000).CurrentItem": str(item)})

    return types.SimpleNamespace(kodi=kodi, hover=hover,
                                 view=lambda: xbmcgui.Window(10000).getProperty("WebOS.HomeView"),
                                 click_flag=lambda: xbmcgui.Window(10000).getProperty("WebOS.PointerClick"))


async def test_pointer_freezes_the_home_section_and_buttons_put_the_menu_back(home):
    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    keeper.track()
    assert home.view() == "movies"           # frozen on what's showing
    home.hover("tvshows", 3)                 # hovering moves the menu selection...
    keeper.track()
    assert home.view() == "movies"           # ...but the home screen stays on Movies
    keeper.key()                             # a button: back to button behaviour
    assert home.kodi.builtins[-1] == "Control.Move(9000,-1)"  # selection back on Movies
    assert home.view() == ""
    keeper.close()


async def test_pointer_click_on_the_menu_switches_section_without_the_library(home):
    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    keeper.track()
    home.hover("music", 4)
    home.kodi.conditions["Control.HasFocus(9000)"] = True
    keeper.click()
    assert home.view() == "music"
    assert home.click_flag() == "true"       # the skin skips the item's library jump
    keeper.key()
    assert home.view() == ""
    assert not any(b.startswith("Control.Move") for b in home.kodi.builtins)  # already on Music
    keeper.close()


async def test_pointer_idling_puts_the_menu_back(home):
    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    keeper.track()
    home.hover("tvshows", 3)
    await asyncio.sleep(0.1)                 # Kodi hides the pointer after a while
    assert home.view() == ""
    assert home.kodi.builtins[-1] == "Control.Move(9000,-1)"
    keeper.close()


async def test_leaving_the_menu_puts_its_highlight_back_on_the_section_shown(home):
    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    keeper.track()
    home.hover("tvshows", 3)
    home.kodi.conditions["Control.HasFocus(9000)"] = True
    keeper.track()                           # on the menu: hovering, nothing moves
    assert not any(b.startswith("Control.Move") for b in home.kodi.builtins)
    home.kodi.conditions["Control.HasFocus(9000)"] = False
    keeper.track()                           # off the menu (it collapses)
    assert home.kodi.builtins[-1] == "Control.Move(9000,-1)"
    assert home.view() == "movies"           # still pointer behaviour
    keeper.close()


async def test_rows_under_the_open_menus_labels_highlight_and_click(home):
    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    keeper.track()                            # pointer in use: Movies (item 2) frozen
    home.kodi.conditions["Control.HasFocus(9100)"] = True  # over the label area
    home.kodi.info_labels.update({"Container(9000).Position": "1", "Container(9000).NumItems": "9"})
    keeper.track(300, 40 + 95 * 3 + 10)       # the fourth row on screen
    assert home.kodi.builtins[-1] == "Control.Move(9000,2)"  # item 2 -> item 4
    home.hover("music", 4)
    keeper.click()
    assert home.view() == "music"
    assert home.click_flag() == "true"
    keeper.track(300, 1000)                   # below the rows (the buttons): leave it
    assert home.kodi.builtins[-1] == "Control.Move(9000,2)"
    keeper.close()


async def test_ok_while_pointing_is_a_click(kodi_side):
    focus = Focus()
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, Mouse(), None, focus)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)
        for msg in ({"pos": [500, 500]}, {"key": 28, "value": 1}, {"key": 28, "value": 0}):
            writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: kodi_side.actions == ["select"])
        assert "click" in focus.calls and "key" not in focus.calls
        # After a D-pad press hides the pointer, OK is a button again.
        for msg in ({"key": 108, "value": 1}, {"key": 108, "value": 0},
                    {"key": 28, "value": 1}, {"key": 28, "value": 0}):
            writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: kodi_side.actions == ["select", "down", "select"])
        assert focus.calls.count("key") == 2 and focus.calls.count("click") == 1
    finally:
        await rb.close()


@pytest.mark.parametrize(("wheel", "item", "expect"), [
    ("wheelup", "1", None),        # first item: scrolling up doesn't leave the row
    ("wheelup", "2", "left"),
    ("wheeldown", "10", None),     # last item
    ("wheeldown", "9", "right"),
])
def test_wheel_stops_at_the_ends_of_a_row(kodi, wheel, item, expect):
    from resources.lib.service import WebOsService

    service = WebOsService()
    kodi.info_labels.update({"System.CurrentControlID": "5100", "Container(5100).NumItems": "10",
                             "Container(5100).CurrentItem": item,
                             "Window(Home).Property(WebOS.WheelSideways)": "Control.HasFocus(5100)"})
    kodi.conditions["Control.HasFocus(5100)"] = True
    service._remote_button(wheel)
    sent = [c["params"]["button"] for c in kodi.jsonrpc_calls if c["method"] == "Input.ButtonEvent"]
    assert sent == ([expect] if expect else [])


async def test_wake_distance_is_a_setting(kodi_side):
    mouse = Mouse()
    rb = RemoteBridge(dataclasses.replace(POINTER, bridge_pointer_wake=12), kodi_side.execute,
                      lambda: True, None, mouse)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)
        for msg in ({"pos": [900, 500]}, {"key": 108, "value": 1}, {"key": 108, "value": 0},
                    {"pos": [905, 503]}, {"pos": [920, 500]}):
            writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: len(mouse.moves) == 2)
        assert mouse.moves[-1][:2] == (920, 500)  # 20 away: past the "very high" 12
    finally:
        await rb.close()


async def test_tv_pointer_is_lined_up_with_the_focus_while_hidden(kodi_side, monkeypatch):
    monkeypatch.setattr(bridge, "PREWARP_DELAY", 0.02)
    mouse = Mouse()
    focus = Focus(where=(480.0, 270.0))
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, mouse, None, focus)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)

        async def send(*msgs):
            for msg in msgs:
                writer.write(json.dumps(msg).encode() + b"\n")
            await writer.drain()
            await asyncio.sleep(0.05)

        await send({"tv": {"cursorAutoRemover": "on"}}, {"pos": [1500, 900]})
        await send({"key": 105, "value": 1}, {"key": 105, "value": 0})   # Left
        assert (await read_until(reader, "warp")) == {"warp": [480, 270]}
        await send({"warped": [480, 270]})       # the TV's pointer is on the item
        await send({"pos": [490, 275]})          # jitter there: still hidden
        assert mouse.moves[-1][:2] == (1500, 900)
        await send({"pos": [700, 400]})          # a real move from the item: no jump
        assert [m[:2] for m in mouse.moves[-1:]] == [(700, 400)]
    finally:
        await rb.close()


async def test_pointer_waits_for_the_tv_when_it_wakes_before_lining_up(kodi_side, monkeypatch):
    monkeypatch.setattr(bridge, "FOCUS_TRACK", 0.0)
    mouse = Mouse()
    focus = Focus(where=(480.0, 270.0))
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, mouse, None, focus)
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        await read_msg(reader)

        async def send(*msgs):
            for msg in msgs:
                writer.write(json.dumps(msg).encode() + b"\n")
            await writer.drain()
            await asyncio.sleep(0.05)

        await send({"tv": {"cursorAutoRemover": "on"}}, {"pos": [1500, 900]})
        await send({"key": 105, "value": 1}, {"key": 105, "value": 0})
        await send({"pos": [1900, 1000]})        # wakes before the TV lined up (asleep)
        assert (await read_until(reader, "warp")) == {"warp": [480, 270]}
        assert mouse.moves[-1][:2] == (1500, 900)  # not shown early, nor at the target
        await send({"pos": [1910, 1005]})        # from before the TV moved its pointer
        assert mouse.moves[-1][:2] == (1500, 900)
        await send({"pos": [490, 275]})          # the TV's pointer arrived: shown there
        assert mouse.moves[-1][:2] == (490, 275)
        # Turned off: it comes back where the remote points.
        rb.update(dataclasses.replace(POINTER, bridge_pointer_return=False))
        await send({"key": 105, "value": 1}, {"key": 105, "value": 0}, {"pos": [1300, 100]})
        assert mouse.moves[-1][:2] == (1300, 100)
    finally:
        await rb.close()


async def test_no_return_to_focus_with_a_bridge_that_cant_move_the_tv_pointer(kodi_side):
    mouse = Mouse()
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, mouse, None,
                      Focus(where=(480.0, 270.0)))
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)  # no "tv" message: bridge 0.2
        await read_msg(reader)
        for msg in ({"pos": [1500, 900]}, {"key": 105, "value": 1}, {"key": 105, "value": 0},
                    {"pos": [1900, 1000]}):
            writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: len(mouse.moves) == 2)
        assert mouse.moves[-1][:2] == (1900, 1000)
    finally:
        await rb.close()


async def test_focus_keeper_learns_where_items_are(kodi):
    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    kodi.info_labels.update({"System.CurrentWindow": "Home", "System.CurrentControlID": "5100",
                             "Container(5100).Position": "0", "Container(5100).NumItems": "9"})
    keeper.track(100, 300)
    kodi.info_labels["Container(5100).Position"] = "1"
    keeper.track(400, 300)
    kodi.info_labels["Container(5100).Position"] = "4"  # the D-pad went on
    assert keeper.spot() == (1300, 300)
    kodi.info_labels["System.CurrentControlID"] = "802"  # never pointed at
    assert keeper.spot() is None
    keeper.close()


@pytest.mark.parametrize(("position", "items", "moves"), [
    ("13", -1, ["Down"] * 6 + ["Up"] * 5),   # third row: down to the edge, on one, back
    ("13", 2, ["Up"] * 4 + ["Down"] * 2),    # up to the top, on two, back
    ("3", 1, []),                            # the first channel's already on top
    ("15", -1, []),                          # the last channels are on screen
])
async def test_guide_wheel_scrolls_the_channels_under_the_pointer(kodi, position, items, moves):
    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    kodi.info_labels.update({"System.CurrentControlID": "50", "Container(50).Position": position,
                             "Container(50).NumItems": "20"})
    keeper.track(800, 247 + 2 * 62 + 30)        # the third row
    assert keeper.wheel(items) is False         # not the guide
    kodi.conditions["Window.IsActive(tvguide) | Window.IsActive(radioguide)"] = True
    assert keeper.wheel(items) is False         # the guide, but not over its channels
    kodi.conditions["Control.HasFocus(50)"] = True
    assert keeper.wheel(items) is True
    assert kodi.builtins == [f"Action({m})" for m in moves]
    keeper.close()


async def test_guide_wheel_over_the_groups_goes_sideways(kodi):
    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    kodi.conditions["Window.IsActive(tvguide) | Window.IsActive(radioguide)"] = True
    kodi.conditions["Control.HasFocus(9102)"] = True
    kodi.info_labels.update({"Container(11).CurrentItem": "2", "Container(11).NumItems": "4"})
    assert keeper.wheel(-1) is True             # down: the next group, on the right
    assert keeper.wheel(-1) is True             # before Kodi catches up
    assert keeper.wheel(-5) is True             # past the last: stays there
    assert keeper.wheel(4) is True              # back to the first
    assert kodi.builtins == ["SetFocus(11,2,absolute)", "SetFocus(11,3,absolute)",
                             "SetFocus(11,0,absolute)"]
    keeper.close()


async def test_fast_guide_wheel_stops_at_the_end_before_kodi_catches_up(kodi):
    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    kodi.conditions["Window.IsActive(tvguide) | Window.IsActive(radioguide)"] = True
    kodi.conditions["Control.HasFocus(50)"] = True
    kodi.info_labels.update({"System.CurrentControlID": "50", "Container(50).Position": "13",
                             "Container(50).NumItems": "20"})
    keeper.track(800, 247 + 2 * 62 + 30)        # the third row: channels 11-18 on screen
    for _ in range(4):                          # Position not updated yet
        keeper.wheel(-1)
    assert kodi.builtins.count("Action(Down)") - kodi.builtins.count("Action(Up)") == 2  # 19, 20
    keeper.close()


async def test_guide_groups_switch_on_click_not_hover(kodi):
    import xbmcgui

    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    kodi.info_labels.update({"System.CurrentWindow": "TV guide", "System.CurrentControlID": "9102",
                             "Container(11).CurrentItem": "3", "Container(11).Position": "0",
                             "Container(11).NumItems": "4"})
    kodi.conditions["Control.HasFocus(9102)"] = True
    home = xbmcgui.Window(10000)
    keeper.track(40 + 368 + 100, 165)          # over the second slot: group 4
    assert home.getProperty("WebOS.PadRow") == "1"
    assert kodi.builtins == []                 # hovering switches nothing
    keeper.track(40 + 2 * 368 + 100, 165)      # past the last group
    assert home.getProperty("WebOS.PadRow") == ""
    keeper.track(60, 165)
    keeper.click()
    assert kodi.builtins == ["SetFocus(11,2,absolute)"]  # group 3 (0-based 2)
    keeper.key()
    assert kodi.builtins[-1] == "SetFocus(11)"
    keeper.close()


async def test_settings_side_menu_switches_on_click_not_hover(kodi):
    import xbmcgui


    from resources.lib.service import FocusKeeper

    keeper = FocusKeeper()
    kodi.info_labels.update({"System.CurrentWindow": "Settings", "System.CurrentControlID": "9101",
                             "Container(3).NumItems": "6"})
    kodi.conditions["Control.HasFocus(9101)"] = True
    home = xbmcgui.Window(10000)
    keeper.track(200, 130 + 2 * 85 + 40)       # over the third category
    assert home.getProperty("WebOS.Pointing") == "true"
    assert home.getProperty("WebOS.PadRow") == "2"
    assert kodi.builtins == []                 # hovering switches nothing
    keeper.track(200, 130 + 7 * 85)            # below the last one
    assert home.getProperty("WebOS.PadRow") == ""
    keeper.track(200, 130 + 4 * 85 + 10)
    keeper.click()
    assert kodi.builtins == ["SetFocus(-196)"]  # Kodi's fifth category button
    kodi.conditions["Window.IsActive(1120) | Window.IsActive(1121)"] = True
    keeper.click()
    assert kodi.builtins[-1] == "SetFocus(25)"   # the LG TV page's own buttons
    keeper.key()                               # buttons: back to the list
    assert kodi.builtins[-1] == "SetFocus(3)"
    assert home.getProperty("WebOS.Pointing") == ""
    keeper.close()


async def test_home_goes_to_kodis_home_and_held_to_the_tvs(kodi_side, monkeypatch):
    monkeypatch.setattr(bridge, "LONG_PRESS", 0.05)
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, Mouse())
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        assert (await read_msg(reader))["grab"] is True

        async def send(*msgs, wait=0.02):
            for msg in msgs:
                writer.write(json.dumps(msg).encode() + b"\n")
            await writer.drain()
            await asyncio.sleep(wait)

        await send({"key": 172, "value": 1}, {"key": 172, "value": 0})
        assert kodi_side.actions == ["kodihome"]
        await send({"key": 172, "value": 1}, wait=0.1)
        await send({"key": 172, "value": 0})
        assert kodi_side.actions == ["kodihome"]
        assert await read_until(reader, "press") == {"press": 172}
    finally:
        await rb.close()


async def test_home_stays_with_the_tv_when_the_remote_isnt_taken_over(kodi_side):
    rb = RemoteBridge(BASE, kodi_side.execute, lambda: True, None, Mouse())
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        assert (await read_msg(reader))["grab"] is False
        for msg in ({"key": 102, "value": 1}, {"key": 102, "value": 0}, {"key": BACK, "value": 1}):
            writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        await wait_for(lambda: kodi_side.actions == ["back"])
    finally:
        await rb.close()


@pytest.mark.parametrize(("speed", "notches", "expect"), [
    (1.0, 3, 3),      # normal: an item per notch
    (0.5, 3, 1),      # medium: two notches per item (the third waits for a fourth)
    (1 / 3, 3, 1),
    (2.0, 2, 4),
])
async def test_wheel_speed(kodi_side, speed, notches, expect):
    mapper = KeyMapper(kodi_side.execute, lambda: True, lambda: speed)
    for _ in range(notches):
        mapper.wheel(-1)
    assert kodi_side.actions == ["wheeldown"] * expect


async def test_slow_wheel_starts_over_on_a_change_of_direction(kodi_side):
    mapper = KeyMapper(kodi_side.execute, lambda: True, lambda: 0.5)
    mapper.wheel(-1)
    mapper.wheel(1)       # half a step down is dropped, not taken off this
    mapper.wheel(1)
    assert kodi_side.actions == ["wheelup"]


async def test_an_older_tv_app_is_reported_as_outdated(kodi_side, monkeypatch):
    states: list[str] = []
    rb = RemoteBridge(BASE, kodi_side.execute, lambda: True, states.append)
    monkeypatch.setattr(rb, "_bundled", {"version": "0.5.0"})
    await rb.start()
    try:
        reader, _writer = await connect(rb)  # says 0.1.0
        await read_msg(reader)
        await wait_for(lambda: rb.status()["remote_bridge_connected"])
        assert rb.status()["remote_bridge_outdated"] is True
        assert rb.status()["remote_bridge_bundled"] == "0.5.0"
        assert states[-1] == "outdated"
    finally:
        await rb.close()


async def test_volume_buttons_go_to_the_receiver_when_asked(kodi_side, monkeypatch):
    monkeypatch.setattr(bridge, "VOLUME_REPEAT", 0.05)
    ready = {"receiver": True}
    settings = dataclasses.replace(POINTER, bridge_volume_receiver=True)
    rb = RemoteBridge(settings, kodi_side.execute, lambda: True, None, Mouse(),
                      receiver_ready=lambda: ready["receiver"])
    await rb.start()
    try:
        reader, writer = await connect_pointer(rb)
        grab = await read_msg(reader)
        assert {113, 114, 115} <= set(grab["consume"])     # taken from the TV

        async def send(*msgs, pause=0.0):
            for msg in msgs:
                writer.write(json.dumps(msg).encode() + b"\n")
                await writer.drain()
                await asyncio.sleep(pause)
            await asyncio.sleep(0.05)

        await send({"key": 115, "value": 1}, {"key": 115, "value": 2}, {"key": 115, "value": 2},
                   {"key": 115, "value": 0})               # a short hold: repeats are spaced
        assert kodi_side.actions == ["receiver:volume_up"]
        await send({"key": 115, "value": 1}, {"key": 115, "value": 2}, pause=0.06)
        assert kodi_side.actions[-1] == "receiver:volume_up" and len(kodi_side.actions) == 3
        await send({"key": 113, "value": 1}, {"key": 113, "value": 2}, {"key": 113, "value": 0})
        assert kodi_side.actions[-1] == "receiver:mute" and len(kodi_side.actions) == 4  # no repeat
        ready["receiver"] = False                           # receiver off: the TV's again
        rb.refresh()
        assert not {113, 114, 115} & set((await read_msg(reader))["consume"])
    finally:
        await rb.close()


async def test_volume_stays_with_the_tv_by_default(kodi_side):
    rb = RemoteBridge(POINTER, kodi_side.execute, lambda: True, None, Mouse(), receiver_ready=lambda: True)
    await rb.start()
    try:
        reader, _writer = await connect_pointer(rb)
        assert not {113, 114, 115} & set((await read_msg(reader))["consume"])
    finally:
        await rb.close()
