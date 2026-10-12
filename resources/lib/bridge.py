"""Remote Bridge: the TV's own remote drives Kodi, a network stand-in for HDMI-CEC.

On a rooted TV the Remote Bridge app (github.com/dangerouslaser/kodi-remote-bridge)
runs a small daemon that reads the remote's input devices and streams every
button press here over TCP. Presses become Kodi actions while Kodi's input is
on screen and are ignored otherwise; the TV keeps handling them as usual.

With the pointer on, Kodi has the daemon take the remote over while Kodi is on
screen ("grab"): the TV then draws no pointer of its own and shows no input
banners, the pointer moves Kodi's mouse, and the daemon hands every button Kodi
doesn't use (volume, Home, power...) back to the TV. The grab message doubles as
a heartbeat; the daemon releases the remote when it stops.

Protocol, one JSON object per line:
    <- {"hello": {"token", "version", "python", "devices", "pointer": [max_x, max_y]}}
    -> {"ok": true}
    <- {"key": code, "value": 0 up | 1 down | 2 repeat, "dev": index}
    <- {"wheel": steps, "dev": index}
    <- {"pos": [x, y]}                  while taken over with the pointer on
    -> {"grab": bool, "consume": [codes], "pointer": bool}   on change and every 2 s
    <- {"cursor": {"point", "hover", "hold", "drag": base64 PNG}}   the TV's own pointer
                                        images, once per connection (see cursor.py)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hmac
import json
from collections.abc import Callable
from typing import Any

from . import kodilog as log
from .settings import Settings

APP_ID = "com.github.dangerouslaser.kodiremote"
HBCHANNEL_ID = "org.webosbrew.hbchannel"
REPO_URL = "https://dangerouslaser.github.io/webos/repo.json"

HELLO_TIMEOUT = 5.0
HEARTBEAT = 2.0
# Pointer speeds other than 1 move the cursor by the remote's motion, scaled;
# a pause this long starts a new stroke from where the cursor is.
STROKE_GAP = 0.4
# The Magic Remote's pointer click (BTN_LEFT); only used with the pointer on.
POINTER_CLICK = 272
# Lines carry the TV's cursor images (a few KB each).
MAX_LINE = 1024 * 1024
CURSOR_MAX = 256 * 1024
# Holding OK this long opens the context menu instead of selecting.
LONG_PRESS = 0.6
# Held arrows repeat only on the TV's own repeat events (every 50 ms after
# 0.5 s on a G5), so nothing can keep repeating once the TV stops sending.
# This caps how fast they move Kodi.
REPEAT_MIN_INTERVAL = 0.08

# Linux input key codes (linux/input-event-codes.h) -> buttons of Kodi's
# remote keymap (remote.xml, "R1"). Kodi resolves them per window, like a
# real remote: OK selects in menus and opens the OSD in fullscreen video, and
# user keymaps apply.
KEY_BUTTONS: dict[int, str] = {
    1: "back",            # KEY_ESC
    2: "one", 3: "two", 4: "three", 5: "four", 6: "five",
    7: "six", 8: "seven", 9: "eight", 10: "nine", 11: "zero",
    28: "select",         # KEY_ENTER (the OK button)
    103: "up", 105: "left", 106: "right", 108: "down",
    119: "pause",         # KEY_PAUSE
    128: "stop",          # KEY_STOP
    139: "menu",          # KEY_MENU
    158: "back",          # KEY_BACK
    163: "skipplus",      # KEY_NEXTSONG
    164: "play",          # KEY_PLAYPAUSE
    165: "skipminus",     # KEY_PREVIOUSSONG
    168: "reverse",       # KEY_REWIND
    174: "back",          # KEY_EXIT
    207: "play",          # KEY_PLAY
    208: "forward",       # KEY_FASTFORWARD
    352: "select",        # KEY_OK
    358: "info",          # KEY_INFO
    365: "guide",         # KEY_EPG
    370: "subtitle",      # KEY_SUBTITLE
    392: "language",      # KEY_AUDIO
    398: "red", 399: "green", 400: "yellow", 401: "blue",
    402: "channelplus",   # KEY_CHANNELUP
    403: "channelminus",  # KEY_CHANNELDOWN
    # LG Magic Remote (seen on a G5, webOS 10)
    412: "back",          # KEY_PREVIOUS: the Back button
    1198: "up",           # scroll wheel up, one press per notch
    1199: "down",         # scroll wheel down
}
REPEATING = frozenset({"up", "down", "left", "right"})
# Held OK: "menu" is the context menu in lists and the OSD in fullscreen video.
LONG_PRESS_BUTTONS = {"select": "menu"}

KEY_UP, KEY_DOWN, KEY_REPEAT = 0, 1, 2


class KeyMapper:
    """Turns press/release events into Kodi remote buttons (loop thread)."""

    def __init__(self, execute: Callable[[str], None], active: Callable[[], bool]) -> None:
        self._execute = execute
        self._active = active
        self._long: dict[int, asyncio.TimerHandle] = {}
        self._long_fired: set[int] = set()
        self._last_repeat: dict[int, float] = {}
        self._unmapped: set[int] = set()

    def key(self, code: int, value: int) -> None:
        action = KEY_BUTTONS.get(code)
        if action is None:
            if value == KEY_DOWN and code not in self._unmapped:
                self._unmapped.add(code)
                log.info(f"remote bridge: button {code} has no Kodi button")
            return
        if value == KEY_DOWN:
            self._press(code, action)
        elif value == KEY_REPEAT:
            self._repeat(code, action)
        elif value == KEY_UP:
            self._release(code, action)

    def wheel(self, steps: int) -> None:
        if not self._active():
            return
        # Up/down rather than scrolling: the remote's wheel moves the focus,
        # the way it does in the TV's own menus.
        action = "up" if steps > 0 else "down"
        for _ in range(min(abs(steps), 5)):
            self._execute(action)

    def release_all(self) -> None:
        for handle in self._long.values():
            handle.cancel()
        self._long.clear()
        self._long_fired.clear()
        self._last_repeat.clear()

    def _press(self, code: int, action: str) -> None:
        self._cancel(code)
        if not self._active():
            return
        if action in LONG_PRESS_BUTTONS:
            loop = asyncio.get_running_loop()
            self._long[code] = loop.call_later(LONG_PRESS, self._long_press, code, action)
            return
        self._execute(action)
        self._last_repeat[code] = asyncio.get_running_loop().time()

    def _repeat(self, code: int, action: str) -> None:
        if action not in REPEATING or code not in self._last_repeat or not self._active():
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_repeat[code] >= REPEAT_MIN_INTERVAL:
            self._last_repeat[code] = now
            self._execute(action)

    def _release(self, code: int, action: str) -> None:
        pending = self._long.pop(code, None)
        if pending is not None:
            pending.cancel()
        long_fired = code in self._long_fired
        self._long_fired.discard(code)
        self._last_repeat.pop(code, None)
        # A short press of a long-press button acts on release.
        if action in LONG_PRESS_BUTTONS and pending is not None and not long_fired and self._active():
            self._execute(action)

    def _long_press(self, code: int, action: str) -> None:
        self._long.pop(code, None)
        self._long_fired.add(code)
        if self._active():
            self._execute(LONG_PRESS_BUTTONS[action])

    def _cancel(self, code: int) -> None:
        handle = self._long.pop(code, None)
        if handle is not None:
            handle.cancel()
        self._long_fired.discard(code)
        self._last_repeat.pop(code, None)


class RemoteBridge:
    """TCP server the TV-side daemon connects to (loop thread)."""

    def __init__(self, settings: Settings, execute: Callable[[str], None],
                 on_screen: Callable[[], bool],
                 on_change: Callable[[str], None] | None = None,
                 mouse: Any = None,
                 on_cursor: Callable[[dict[str, bytes]], None] | None = None) -> None:
        self.settings = settings
        self.mapper = KeyMapper(execute, on_screen)
        self._on_screen = on_screen
        self._on_change = on_change
        self._mouse = mouse
        self._on_cursor = on_cursor
        self._published: str | None = None
        self._heartbeat: asyncio.Task | None = None
        self._server: asyncio.Server | None = None
        self._clients: dict[asyncio.StreamWriter, dict[str, Any]] = {}
        self._listening: tuple[bool, int, str] | None = None

    async def start(self) -> None:
        await self._apply()

    def update(self, settings: Settings) -> None:
        self.settings = settings
        self.refresh()
        task = asyncio.ensure_future(self._apply())
        task.add_done_callback(_log_error)

    async def close(self) -> None:
        await self._stop()
        if self._mouse is not None:
            self._mouse.close()

    def status(self) -> dict[str, Any]:
        client = next(iter(self._clients.values()), None)
        return {
            "remote_bridge": self.settings.remote_bridge,
            "remote_bridge_listening": self._server is not None,
            "remote_bridge_connected": client is not None,
            "remote_bridge_version": (client or {}).get("version", ""),
        }

    def refresh(self) -> None:
        """TV state changed: take the remote over or hand it back right away."""
        for writer, info in self._clients.items():
            if info.get("grab") != self._grab_wanted():
                self._send_grab(writer, info)

    def _grab_wanted(self) -> bool:
        return self.settings.bridge_pointer and self._on_screen()

    def _send_grab(self, writer: asyncio.StreamWriter, info: dict[str, Any]) -> None:
        grab = self._grab_wanted()
        info["grab"] = grab
        consume = sorted({*KEY_BUTTONS, POINTER_CLICK})
        msg = {"grab": grab, "consume": consume, "pointer": self.settings.bridge_pointer}
        with contextlib.suppress(OSError, RuntimeError):
            writer.write(json.dumps(msg).encode() + b"\n")

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT)
            for writer, info in list(self._clients.items()):
                self._send_grab(writer, info)
            if self._mouse is not None and self.settings.bridge_pointer:
                self._mouse.ping()

    @property
    def state(self) -> str:
        """"connected", "waiting" (listening for the TV) or "" (off)."""
        if self._clients:
            return "connected"
        return "waiting" if self._server is not None else ""

    def _publish(self) -> None:
        state = self.state
        if state != self._published and self._on_change is not None:
            self._published = state
            self._on_change(state)

    async def _apply(self) -> None:
        wanted = (self.settings.remote_bridge, self.settings.bridge_port, self.settings.bridge_token)
        if wanted == self._listening:
            return
        await self._stop()
        self._listening = wanted
        enabled, port, token = wanted
        if not (enabled and token):
            self._publish()
            return
        try:
            self._server = await asyncio.start_server(self._client, host="0.0.0.0", port=port,
                                                      limit=MAX_LINE)
        except OSError as err:
            log.warning(f"remote bridge: cannot listen on port {port}: {err}")
            self._listening = None
            self._publish()
            return
        log.info(f"remote bridge: listening on port {port}")
        self._heartbeat = asyncio.ensure_future(self._beat())
        self._publish()

    async def _stop(self) -> None:
        self._listening = None
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            self._heartbeat = None
        server, self._server = self._server, None
        for writer in list(self._clients):
            writer.close()
        self._clients.clear()
        self.mapper.release_all()
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 1.0)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            hello = await self._hello(reader)
            if hello is None:
                log.warning(f"remote bridge: rejected a connection from {peer} (bad hello or token)")
                return
            writer.write(b'{"ok": true}\n')
            await writer.drain()
            info = {"version": str(hello.get("version", "")), "peer": peer,
                    "pointer": _pointer_range(hello.get("pointer"))}
            self._clients[writer] = info
            self._send_grab(writer, info)
            self._publish()
            log.info(f"remote bridge: TV at {peer} connected (bridge {hello.get('version', '?')}, "
                     f"{_runtime(hello)}, devices {hello.get('devices', [])})")
            while True:
                line = await reader.readline()
                if not line:
                    break
                self._message(line, info)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError,
                asyncio.TimeoutError):
            pass
        finally:
            if self._clients.pop(writer, None) is not None:
                log.info(f"remote bridge: TV at {peer} disconnected")
                self.mapper.release_all()
                self._publish()
            writer.close()

    async def _hello(self, reader: asyncio.StreamReader) -> dict[str, Any] | None:
        line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
        try:
            hello = json.loads(line).get("hello")
        except (ValueError, AttributeError):
            return None
        if not isinstance(hello, dict):
            return None
        token = str(hello.get("token", ""))
        if not hmac.compare_digest(token.encode(), self.settings.bridge_token.encode()):
            return None
        return hello

    def _message(self, line: bytes, info: dict[str, Any]) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if "key" in msg:
            try:
                code, value = int(msg["key"]), int(msg.get("value", 1))
            except (TypeError, ValueError):
                return
            log.debug(f"remote bridge: key {code} {value} (device {msg.get('dev')})")
            if code == POINTER_CLICK:
                if self.settings.bridge_pointer:
                    self.mapper.key(28, value)  # a click selects what the pointer is over
                return
            self.mapper.key(code, value)
        elif "cursor" in msg:
            if self._on_cursor is not None and isinstance(msg["cursor"], dict):
                self._on_cursor(_decode_images(msg["cursor"]))
        elif "pos" in msg:
            if self._mouse is None or not self.settings.bridge_pointer or not self._on_screen():
                return
            try:
                x, y = (int(v) for v in msg["pos"])
            except (TypeError, ValueError):
                return
            max_x, max_y = info["pointer"]
            cx, cy = self._cursor(info, x, y, max_x, max_y)
            self._mouse.move(round(cx), round(cy), max_x, max_y)
        elif "wheel" in msg:
            with contextlib.suppress(TypeError, ValueError):
                self.mapper.wheel(int(msg["wheel"]))


    def _cursor(self, info: dict[str, Any], x: int, y: int, max_x: int, max_y: int) -> tuple[float, float]:
        """Where Kodi's cursor goes for the remote's absolute position x, y."""
        speed = self.settings.bridge_pointer_speed
        now = asyncio.get_running_loop().time()
        last, last_at, cursor = info.get("raw"), info.get("raw_at", 0.0), info.get("cursor")
        info["raw"], info["raw_at"] = (x, y), now
        if speed == 1.0 or cursor is None:
            cursor = (float(x), float(y))
        elif last is not None and now - last_at <= STROKE_GAP:
            cursor = (min(max(cursor[0] + (x - last[0]) * speed, 0.0), float(max_x)),
                      min(max(cursor[1] + (y - last[1]) * speed, 0.0), float(max_y)))
        # else: a new stroke starts where the cursor already is
        info["cursor"] = cursor
        return cursor


def _decode_images(raw: dict[str, Any]) -> dict[str, bytes]:
    images = {}
    for kind, text in raw.items():
        if not isinstance(text, str) or len(text) > CURSOR_MAX * 4 // 3 + 4:
            continue
        with contextlib.suppress(binascii.Error, ValueError):
            images[str(kind)] = base64.b64decode(text, validate=True)
    return images


def _pointer_range(value: Any) -> tuple[int, int]:
    try:
        x, y = (int(v) for v in value)
    except (TypeError, ValueError):
        return 1920, 1080
    return (x if x > 0 else 1920), (y if y > 0 else 1080)


def _runtime(hello: dict[str, Any]) -> str:
    if hello.get("python"):
        return f"python {hello['python']}"
    return f"node {hello.get('node', '?')}"


def _log_error(task: asyncio.Future) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning(f"remote bridge: {task.exception()!r}")
