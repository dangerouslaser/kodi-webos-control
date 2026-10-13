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
    <- {"cursor": {"point", "hover", "hold", "drag": base64 PNG}, "hotspot": rule}
                                        the TV's own pointer images, on connecting
                                        and when its size or shape changes (cursor.py)
    <- {"tv": {setting: value}}         the TV's pointer settings (settings.TV_POINTER),
                                        on connecting and on every change (bridge 0.3)
    -> {"set_tv": {setting: value}}     change them on the TV
    -> {"warp": [x, y]}                 move the TV's pointer (the remote's position)
    <- {"warped": [x, y]}               done (bridge 0.4.2)

The pointer moves 1:1 with the TV's: its speed, alignment and auto-hide are
the TV's own pointer settings, changed on the TV from here.
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

from . import bridgerepo
from . import kodilog as log
from .remotes import KODI_BUTTONS, KODI_HOME, Remote, button_name, kodi_buttons
from .settings import Settings

APP_ID = "com.github.dangerouslaser.kodiremote"
HBCHANNEL_ID = "org.webosbrew.hbchannel"
# The TV app this add-on carries is served to the Homebrew Channel on the
# bridge port + this (see bridgerepo.py).
REPO_PORT_OFFSET = 1
# The first TV app that can update itself ({"action": "update"}).
SELF_UPDATE_SINCE = (0, 5, 0)

HELLO_TIMEOUT = 5.0
HEARTBEAT = 2.0
# After a button press the pointer stays hidden until the remote moves
# settings.bridge_pointer_wake (in the TV's 1920-wide coordinates), so hand
# jitter doesn't bring it back. It comes back on what the buttons focused:
# once they've settled (PREWARP_DELAY), the TV's pointer is moved there while
# Kodi's stays hidden, and the TV confirms ("warped"), so the pointer wakes
# from there with no jump. If that didn't happen (the remote was asleep),
# it's moved on waking, and Kodi's stays hidden until the TV's arrives near
# there (or WARP_TIME passes).
PREWARP_DELAY = 0.25
WARP_TIME = 0.6
WARP_NEAR = 120
# The pointer counts as resting after this long without moving. Over empty
# space nothing has focus, and Kodi would hand the next action to the window's
# default control (on Estuary's home screen: the side menu, which then opens),
# so focus goes back to the control the pointer was last over.
POINTER_REST = 0.4
# While the pointer moves, what it's over is noted at most this often.
FOCUS_TRACK = 0.1
# Kodi hides its mouse after this long without movement; till then OK counts
# as a pointer click (the Magic Remote sends OK, not BTN_LEFT, on some TVs).
POINTER_VISIBLE = 5.0
OK_KEYS = frozenset({28, 352})
# The Magic Remote's pointer click (BTN_LEFT); only used with the pointer on.
POINTER_CLICK = 272
# Lines carry the TV's cursor images (a few KB each).
MAX_LINE = 1024 * 1024
CURSOR_MAX = 256 * 1024
# Wheel notches in one direction this close together add up (the slower wheel
# speeds need several per item); a longer pause starts over.
WHEEL_GAP = 1.0
# The virtual wheel sends one notch per event, this far apart (a frame or two):
# Kodi takes one scroll step from each wheel event however many notches it
# carries. (Stock Kodi's settings panes still lose notches that come mid-scroll;
# the yblod builds patch that, kodi-9997-grouplist-wheel-destination.)
NOTCH_GAP = 0.04
# Holding OK this long opens the context menu instead of selecting.
LONG_PRESS = 0.6
# Held arrows repeat only on the TV's own repeat events (every 50 ms after
# 0.5 s on a G5), so nothing can keep repeating once the TV stops sending.
# This caps how fast they move Kodi.
REPEAT_MIN_INTERVAL = 0.08

# Not remote buttons: the service scrolls the focused list along its own
# direction (left/right in a horizontal row).
WHEEL_UP, WHEEL_DOWN = "wheelup", "wheeldown"
# Home: Kodi's home screen; held, the TV's (the bridge presses Home for the
# TV: TV_KEY + code). Only while the remote is taken over: otherwise the TV
# gets Home anyway.
TV_KEY = "tvkey:"
HOME_KEYS = frozenset({102, 172, 773})  # KEY_HOME, KEY_HOMEPAGE, the Magic Remote's Home
# Volume and mute (KEY_VOLUMEUP, KEY_VOLUMEDOWN, KEY_MUTE): normally the TV's.
# With "Volume buttons control the receiver" (and service.denonavr up), Kodi
# takes them while it's on screen and the receiver gets them (RECEIVER + name).
RECEIVER_KEYS = {115: "volume_up", 114: "volume_down", 113: "mute"}
RECEIVER = "receiver:"
# A held volume button: one step this often (the TV repeats every 50 ms).
VOLUME_REPEAT = 0.15

# Linux key code -> Kodi remote button: the shared LG table (remotes.py); the
# bridge narrows it to the paired remote (KeyMapper.buttons). The LG Magic
# Remote's wheel is REL_WHEEL ({"wheel"}); 1198/1199 are its pointer
# waking/idling, which the bridge keeps for the TV.
KEY_BUTTONS = KODI_BUTTONS
REPEATING = frozenset({"up", "down", "left", "right"})
# Held OK: "menu" is the context menu in lists and the OSD in fullscreen video.
LONG_PRESS_BUTTONS = {"select": "menu", KODI_HOME: TV_KEY}

KEY_UP, KEY_DOWN, KEY_REPEAT = 0, 1, 2


class KeyMapper:
    """Turns press/release events into Kodi remote buttons (loop thread)."""

    def __init__(self, execute: Callable[[str], None], active: Callable[[], bool],
                 wheel_speed: Callable[[], float] = lambda: 1.0) -> None:
        self._execute = execute
        self._active = active
        self._wheel_speed = wheel_speed  # items per notch
        self._wheel = (0.0, 0.0)  # notches not yet acted on (signed), last notch time
        self._long: dict[int, asyncio.TimerHandle] = {}
        self._long_fired: set[int] = set()
        self._last_repeat: dict[int, float] = {}
        self._unmapped: set[int] = set()
        self.buttons: dict[int, str] = dict(KODI_BUTTONS)  # what Kodi takes from the paired remote

    def key(self, code: int, value: int) -> None:
        action = self.buttons.get(code)
        if action is None:
            if value == KEY_DOWN and code not in self._unmapped:
                self._unmapped.add(code)
                log.info(f"remote bridge: button {code} ({button_name(code)}) is the TV's")
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
        items = self.wheel_items(steps)
        action = WHEEL_UP if items > 0 else WHEEL_DOWN
        for _ in range(abs(items)):
            self._execute(action)

    def wheel_items(self, steps: int) -> int:
        """Items to scroll for these notches at the wheel speed (> 0: up)."""
        now = asyncio.get_running_loop().time()
        pending, last = self._wheel
        if now - last > WHEEL_GAP or pending * steps < 0:
            pending = 0.0  # a pause or a change of direction starts over
        pending += steps * self._wheel_speed()
        items = int(pending)  # toward zero: whole items only
        self._wheel = (pending - items, now)
        return max(-5, min(5, items))

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
            long_action = LONG_PRESS_BUTTONS[action]
            self._execute(f"{TV_KEY}{code}" if long_action == TV_KEY else long_action)

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
                 on_cursor: Callable[[dict[str, bytes], str], None] | None = None,
                 focus: Any = None,
                 on_tv: Callable[[dict[str, str]], None] | None = None,
                 wheel: Any = None,
                 receiver_ready: Callable[[], bool] = lambda: False,
                 on_activity: Callable[[], None] = lambda: None,
                 on_remote: Callable[[str], None] = lambda label: None) -> None:
        self.settings = settings
        # The TV is awake: its bridge connected or a remote key came in.
        self._on_activity = on_activity
        self._on_remote = on_remote  # the paired remote's label ("LG MR20"), "" when unknown
        self._receiver_ready = receiver_ready  # service.denonavr is connected to a receiver that's on
        self._volume_at = 0.0
        self.mapper = KeyMapper(self._run, on_screen, lambda: self.settings.bridge_wheel_speed)
        self._execute = execute
        self._on_screen = on_screen
        self._on_change = on_change
        self._mouse = mouse
        self._on_cursor = on_cursor
        # .track() while the pointer moves, .restore() when it rests, .spot()
        # where the focused item is (skin coordinates) or None.
        self._focus = focus
        self._on_tv = on_tv  # the TV's pointer settings, as reported
        # .wheel(x, y, notches) at a screen position, False if it can't
        # (uinput.VirtualWheel): scrolls what's under the pointer.
        self._wheel = wheel
        self._notch_at = 0.0  # when the virtual wheel may send its next notch
        self._tracked_at = 0.0
        self._resting: asyncio.TimerHandle | None = None
        self._published: str | None = None
        self._heartbeat: asyncio.Task | None = None
        self._server: asyncio.Server | None = None
        self._clients: dict[asyncio.StreamWriter, dict[str, Any]] = {}
        self._listening: tuple[bool, int, str] | None = None
        self._repo = bridgerepo.RepoServer()
        self._bundled = bridgerepo.bundled()

    async def start(self) -> None:
        await self._apply()

    def _run(self, action: str) -> None:
        if action.startswith(TV_KEY):
            for writer, info in self._clients.items():
                if info.get("grab"):
                    log.info(f"remote bridge: button {action[len(TV_KEY):]} for the TV")
                    self._write(writer, {"press": int(action[len(TV_KEY):])})
            return
        self._execute(action)

    def update(self, settings: Settings) -> None:
        self.settings = settings
        self.refresh()
        self._push_tv_settings()
        task = asyncio.ensure_future(self._apply())
        task.add_done_callback(_log_error)

    async def close(self) -> None:
        await self._stop()
        await self._repo.close()
        if self._mouse is not None:
            self._mouse.close()
        if self._wheel is not None:
            self._wheel.close()

    def status(self) -> dict[str, Any]:
        client = next(iter(self._clients.values()), None)
        return {
            "remote_bridge": self.settings.remote_bridge,
            "remote_bridge_listening": self._server is not None,
            "remote_bridge_connected": client is not None,
            "remote_bridge_version": (client or {}).get("version", ""),
            "remote_bridge_bundled": (self._bundled or {}).get("version", ""),
            "remote_bridge_outdated": client is not None and self._outdated(client),
        }

    def _outdated(self, info: dict[str, Any]) -> bool:
        """The TV's app is older than the one this add-on carries."""
        if self._bundled is None:
            return False
        return (bridgerepo.version_tuple(info.get("version", ""))
                < bridgerepo.version_tuple(self._bundled["version"]))

    def refresh(self) -> None:
        """TV state changed: take the remote over or hand it back right away."""
        for writer, info in self._clients.items():
            if info.get("grab") != self._grab_wanted() or info.get("consume") != self._consume():
                self._send_grab(writer, info)

    def _grab_wanted(self) -> bool:
        return (self.settings.bridge_pointer or self._volume_to_receiver()) and self._on_screen()

    def _volume_to_receiver(self) -> bool:
        return self.settings.bridge_volume_receiver and self._receiver_ready()

    def _consume(self) -> list[int]:
        codes = {*self.mapper.buttons, POINTER_CLICK}
        if self._volume_to_receiver():
            codes |= set(RECEIVER_KEYS)
        return sorted(codes)

    def _send_grab(self, writer: asyncio.StreamWriter, info: dict[str, Any]) -> None:
        grab = self._grab_wanted()
        info["grab"] = grab
        consume = info["consume"] = self._consume()
        msg = {"grab": grab, "consume": consume, "pointer": self.settings.bridge_pointer}
        with contextlib.suppress(OSError, RuntimeError):
            writer.write(json.dumps(msg).encode() + b"\n")

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT)
            for writer, info in list(self._clients.items()):
                self._send_grab(writer, info)
                self._keep_pointer(info)
            if self._mouse is not None and self.settings.bridge_pointer:
                self._mouse.ping()

    def _keep_pointer(self, info: dict[str, Any]) -> None:
        """The TV's auto-hide off: Kodi hides an idle mouse after 5 s; nudge it
        by a pixel and back so it stays up till a button hides it."""
        if (info.get("tv", {}).get("cursorAutoRemover") != "off" or self._mouse is None
                or not self._pointer_visible(info) or not self._on_screen()):
            return
        x, y = info["shown"]
        max_x, max_y = info["pointer"]
        info["nudge"] = not info.get("nudge")
        self._mouse.move(x + (1 if info["nudge"] else 0), y, max_x, max_y)
        info["shown_at"] = asyncio.get_running_loop().time()

    def _push_tv_settings(self) -> None:
        """Settings changed here that differ from what the TV reported: set them there."""
        wanted = dict(self.settings.tv_pointer)
        for writer, info in self._clients.items():
            tv = info.get("tv")
            if tv is None:
                continue  # not reported yet (or an older bridge): the TV's win
            changes = {k: v for k, v in wanted.items() if k in tv and tv[k] != v}
            if changes:
                log.info(f"remote bridge: TV pointer settings {changes}")
                tv.update(changes)  # the TV echoes them back
                self._write(writer, {"set_tv": changes})

    def _write(self, writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        with contextlib.suppress(OSError, RuntimeError):
            writer.write(json.dumps(msg).encode() + b"\n")

    @property
    def state(self) -> str:
        """"connected", "outdated" (connected, older than the app this add-on
        carries), "waiting" (listening for the TV) or "" (off)."""
        if self._clients:
            return "outdated" if any(self._outdated(i) for i in self._clients.values()) else "connected"
        return "waiting" if self._server is not None else ""

    def _publish(self) -> None:
        state = self.state
        if state != self._published and self._on_change is not None:
            self._published = state
            self._on_change(state)

    async def _apply(self) -> None:
        # Served even with the bridge off: setting it up installs the app first.
        port = self.settings.bridge_port
        await self._repo.start(port + REPO_PORT_OFFSET if port else 0)  # 0: any (tests)
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
            info = {"version": str(hello.get("version", "")), "peer": peer, "writer": writer,
                    "pointer": _pointer_range(hello.get("pointer"))}
            self._clients[writer] = info
            self._send_grab(writer, info)
            self._publish()
            self._on_activity()
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
                self._on_remote("")
                self._publish()
            writer.close()

    def _remote(self, remote: Remote, info: dict[str, Any]) -> None:
        """The TV says which Magic Remote is paired: take its buttons."""
        if remote == info.get("remote"):
            return
        info["remote"] = remote
        log.info(f"remote bridge: {remote.label or 'no Magic Remote paired'}"
                 + (f" (number keys: {remote.numeric}, mute key: {remote.mute})"
                    if remote.numeric is not None or remote.mute is not None else ""))
        self.mapper.buttons = kodi_buttons(remote)
        self._send_grab(info["writer"], info)  # what Kodi takes may have changed
        self._on_remote(remote.label)

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
            self._on_activity()
            if info.get("overlay"):
                return  # the TV's own UI is up (the bridge has handed it the remote)
            if code in RECEIVER_KEYS and RECEIVER_KEYS[code] and code in info.get("consume", ()):
                self._receiver_key(code, value, info)
                return
            if code in HOME_KEYS and not info.get("grab"):
                return  # the TV has it (and shows its home)
            if code in OK_KEYS and self._pointer_visible(info):
                code = POINTER_CLICK  # pointing: OK is a click
            if code == POINTER_CLICK:
                if self.settings.bridge_pointer:
                    if value == KEY_DOWN and self._focus is not None and self._on_screen():
                        self._focus.click()
                    self.mapper.key(28, value)  # a click selects what the pointer is over
                return
            if value == KEY_DOWN:
                self._pointer_rest()  # before the button, so it reaches a focused control
                if self._focus is not None and code in self.mapper.buttons and self._on_screen():
                    self._focus.key()
            if value == KEY_DOWN and code in self.mapper.buttons:
                self._hide_pointer(info)
            self.mapper.key(code, value)
        elif "overlay" in msg:
            state = str(msg["overlay"] or "none")
            overlay = state not in ("none", "spinner")  # as the bridge (0.6.3) decides
            if overlay != bool(info.get("overlay")):
                log.info(f"remote bridge: TV UI {state}" + (": the TV has the remote" if overlay else ""))
                if overlay:
                    self.mapper.release_all()  # nothing held on while the TV has it
            info["overlay"] = overlay
        elif "remote" in msg:
            self._remote(Remote.from_report(msg["remote"]), info)
        elif "tv" in msg:
            if isinstance(msg["tv"], dict):
                tv = {str(k): str(v) for k, v in msg["tv"].items()}
                info["tv"] = tv
                log.info(f"remote bridge: TV pointer settings {tv}")
                if self._on_tv is not None:
                    self._on_tv(tv)
        elif "warped" in msg:
            with contextlib.suppress(TypeError, ValueError):
                spot = tuple(int(v) for v in msg["warped"])
                if info.get("hidden_at") is not None and spot == info.get("prewarp"):
                    info["hidden_at"] = spot  # wakes from the focused item
                    info["prewarped"] = True
        elif "cursor" in msg:
            if self._on_cursor is not None and isinstance(msg["cursor"], dict):
                self._on_cursor(_decode_images(msg["cursor"]), str(msg.get("hotspot") or "tip"))
        elif "pos" in msg:
            if self._mouse is None or not self.settings.bridge_pointer or not self._on_screen():
                return
            try:
                x, y = (int(v) for v in msg["pos"])
            except (TypeError, ValueError):
                return
            loop = asyncio.get_running_loop()
            max_x, max_y = info["pointer"]
            info["raw"] = (x, y)
            hidden_at = info.get("hidden_at")
            if hidden_at is not None:
                wake = self.settings.bridge_pointer_wake
                if abs(x - hidden_at[0]) + abs(y - hidden_at[1]) < wake:
                    return
                info.pop("hidden_at")
                target = None if info.pop("prewarped", False) else self._focused_spot(info, max_x, max_y)
                if target is not None:
                    info["warp"] = (*target, loop.time() + WARP_TIME)
                    self._write(info["writer"], {"warp": list(target)})
            warp = info.get("warp")
            if warp is not None:
                tx, ty, until = warp
                if abs(x - tx) + abs(y - ty) >= WARP_NEAR and loop.time() < until:
                    return  # from before the TV moved its pointer
                info.pop("warp")
            self._show_pointer(info, x, y)
            if self._focus is not None and loop.time() - self._tracked_at >= FOCUS_TRACK:
                self._tracked_at = loop.time()
                # In Kodi's 1920x1080 skin coordinates.
                self._focus.track(x * 1920 / max_x, y * 1080 / max_y)
            if self._resting is not None:
                self._resting.cancel()
            self._resting = loop.call_later(POINTER_REST, self._pointer_rest)
        elif "wheel" in msg:
            with contextlib.suppress(TypeError, ValueError):
                if self._wheel_at_pointer(info, int(msg["wheel"])):
                    return
                self._hide_pointer(info)
                if self._focus is not None and self._on_screen():
                    self._focus.key()
                self.mapper.wheel(int(msg["wheel"]))


    def _receiver_key(self, code: int, value: int, info: dict[str, Any]) -> None:
        """A volume or mute button we took from the TV: to the receiver. Held
        volume steps at VOLUME_REPEAT; mute doesn't repeat. The pointer stays."""
        if not info.get("grab") or not self._on_screen():
            return
        now = asyncio.get_running_loop().time()
        if value == KEY_DOWN or (value == KEY_REPEAT and code != 113 and now - self._volume_at >= VOLUME_REPEAT):
            self._volume_at = now
            self._execute(RECEIVER + RECEIVER_KEYS[code])

    def _wheel_at_pointer(self, info: dict[str, Any], steps: int) -> bool:
        """Pointing: scroll what's under the pointer, like a mouse wheel, and
        keep the pointer up. False when the pointer is hidden (the wheel then
        moves the focus) or there's no virtual wheel."""
        shown = info.get("shown")
        if shown is None or not self._pointer_visible(info) or not self._on_screen():
            return False
        items = self.mapper.wheel_items(steps)
        if self._focus is not None and self._focus.wheel(items):
            return True  # the add-on scrolled it (the TV guide's channels)
        max_x, max_y = info["pointer"]
        if self._wheel is None or (items and not self._scroll(shown[0] / max_x, shown[1] / max_y, items)):
            return False
        info["shown_at"] = asyncio.get_running_loop().time()  # a wheel keeps Kodi's mouse up too
        return True

    def _scroll(self, x: float, y: float, items: int) -> bool:
        """Turn the virtual wheel `items` notches at (x, y), one event each,
        NOTCH_GAP apart (after any still on their way). False if it failed."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        step = 1 if items > 0 else -1
        at, left = max(now, self._notch_at), abs(items)
        if at <= now:
            if not self._wheel.wheel(x, y, step):
                return False
            at, left = now + NOTCH_GAP, left - 1
        for _ in range(left):
            loop.call_later(at - now, self._notch, x, y, step)
            at += NOTCH_GAP
        self._notch_at = at
        return True

    def _notch(self, x: float, y: float, step: int) -> None:
        if self._wheel is not None:
            self._wheel.wheel(x, y, step)

    def _hide_pointer(self, info: dict[str, Any]) -> None:
        """Kodi hides its mouse on a button press; keep it hidden till the remote
        moves, and line the TV's pointer up with the focus once it settles."""
        far = -10 * self.settings.bridge_pointer_wake
        if not info.get("prewarped"):
            info["hidden_at"] = info.get("raw") or (far, far)
        info.pop("warp", None)
        info.pop("prewarp", None)
        info["prewarped"] = False
        if info.get("prewarp_timer") is not None:
            info["prewarp_timer"].cancel()
        info["prewarp_timer"] = asyncio.get_running_loop().call_later(
            PREWARP_DELAY, self._prewarp, info)

    def _prewarp(self, info: dict[str, Any]) -> None:
        info["prewarp_timer"] = None
        if info.get("hidden_at") is None or info.get("writer") is None:
            return  # already back
        target = self._focused_spot(info, *info["pointer"])
        if target is not None:
            info["prewarp"] = target
            self._write(info["writer"], {"warp": list(target)})

    def _show_pointer(self, info: dict[str, Any], x: int, y: int) -> None:
        max_x, max_y = info["pointer"]
        self._mouse.move(x, y, max_x, max_y)
        info["shown"] = (x, y)
        info["shown_at"] = asyncio.get_running_loop().time()

    def _focused_spot(self, info: dict[str, Any], max_x: int, max_y: int) -> tuple[int, int] | None:
        """Where the buttons left focus, in the remote's coordinates (or None)."""
        if not (self.settings.bridge_pointer_return and self._focus is not None and self._on_screen()):
            return None
        if "tv" not in info:
            return None  # a bridge before 0.3 can't move the TV's pointer
        spot = self._focus.spot()
        if spot is None:
            return None
        return round(spot[0] * max_x / 1920), round(spot[1] * max_y / 1080)

    def _pointer_visible(self, info: dict[str, Any]) -> bool:
        """Whether Kodi is showing the pointer: moved lately, and not since hidden by a button."""
        if not self.settings.bridge_pointer or info.get("hidden_at") is not None:
            return False
        shown_at = info.get("shown_at")
        return shown_at is not None and asyncio.get_running_loop().time() - shown_at < POINTER_VISIBLE

    def _pointer_rest(self) -> None:
        """The pointer stopped (or a button came first): refocus, once per movement."""
        if self._resting is None:
            return
        self._resting.cancel()
        self._resting = None
        if self._focus is not None and self._on_screen():
            self._focus.restore()


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
