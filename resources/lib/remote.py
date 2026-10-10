"""Transparent control: forward Kodi remote presses to the TV.

While the TV shows another input or app, Kodi is off screen but still gets
key presses from its own remote (IR, Bluetooth, CEC, a phone app). An
invisible modal dialog catches those actions so Kodi doesn't navigate
blind, and sends the matching button to the TV instead. Kodi's context-menu
key switches the TV back to Kodi.

Kodi side only: no asyncio here. ``forward(action, args)`` hands an action
to the service, which runs it on the loop.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import xbmc
import xbmcgui

from . import kodilog as log

# Kodi action ids (xbmcgui.ACTION_*), listed numerically so the mapping
# doesn't depend on which constants a Kodi version exports.
_BUTTONS = {
    1: "LEFT",
    2: "RIGHT",
    3: "UP",
    4: "DOWN",
    7: "ENTER",  # select
    100: "ENTER",  # mouse click
    9: "BACK",  # parent dir
    10: "BACK",  # previous menu
    92: "BACK",  # nav back
    11: "INFO",
    12: "PAUSE",
    13: "STOP",
    68: "PLAY",
    77: "FASTFORWARD",
    78: "REWIND",
    184: "CHANNELUP",
    185: "CHANNELDOWN",
    5: "CHANNELUP",  # page up
    6: "CHANNELDOWN",  # page down
    25: "CC",  # show subtitles
    19: "ASPECT_RATIO",
    215: "RED",
    216: "GREEN",
    217: "YELLOW",
    218: "BLUE",
    **{58 + n: str(n) for n in range(10)},  # REMOTE_0..9
}
_VOLUME = {88: ("volume", ["up"]), 89: ("volume", ["down"]), 91: ("mute", ["toggle"])}
PLAY_PAUSE = 229
CONTEXT_MENU = 117
SHOW_OSD = 24


def translate(action_id: int, kodi_input: str) -> tuple[str, list[str]] | None:
    """Map a Kodi action id to a service action, or None to ignore it."""
    if action_id in _BUTTONS:
        return "button", [_BUTTONS[action_id]]
    if action_id in _VOLUME:
        return _VOLUME[action_id]
    if action_id == SHOW_OSD:
        return "button", ["MENU"]
    if action_id == CONTEXT_MENU and kodi_input:
        return "input", [kodi_input]
    return None


class _Catcher(xbmcgui.WindowDialog):
    """Full-screen, control-less dialog: invisible, but modal."""

    def __init__(self, on_action: Callable[[int], None]) -> None:
        super().__init__()
        self._on_action = on_action

    def onAction(self, action: Any) -> None:  # noqa: N802
        self._on_action(action.getId())


class Overlay:
    """Opens/closes the catcher dialog from any thread."""

    def __init__(
        self,
        forward: Callable[[str, list[str]], None],
        kodi_input: Callable[[], str],
    ) -> None:
        self._forward = forward
        self._kodi_input = kodi_input
        self._lock = threading.Lock()
        self._window: _Catcher | None = None
        self._thread: threading.Thread | None = None
        self._closing = False
        # webOS has no play/pause toggle button; alternate PAUSE and PLAY.
        self._sent_pause = False

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_active(self, active: bool) -> None:
        if active:
            self.open()
        else:
            self.close()

    def open(self) -> None:
        with self._lock:
            if self.active:
                return
            self._closing = False
            self._thread = threading.Thread(target=self._modal, name="webostv-remote", daemon=True)
            self._thread.start()

    def _modal(self) -> None:
        # Kodi runs a window's onAction on the thread that created the
        # window, and only while that thread sits in doModal(). So create
        # and show it here, never on the asyncio loop thread.
        window = _Catcher(self._handle)
        with self._lock:
            if self._closing:
                return
            self._window = window
        # Don't let the screensaver eat the first key press.
        xbmc.executebuiltin("InhibitScreensaver(true)")
        try:
            window.doModal()
        finally:
            xbmc.executebuiltin("InhibitScreensaver(false)")
            with self._lock:
                if self._window is window:
                    self._window = None
            del window

    def close(self) -> None:
        with self._lock:
            self._closing = True
            window, self._window = self._window, None
        if window is not None:
            window.close()

    def _handle(self, action_id: int) -> None:
        if action_id == PLAY_PAUSE:
            self._sent_pause = not self._sent_pause
            mapped = ("button", ["PAUSE" if self._sent_pause else "PLAY"])
        else:
            mapped = translate(action_id, self._kodi_input())
        if mapped is None:
            log.debug(f"transparent control: ignoring Kodi action {action_id}")
            return
        name, args = mapped
        log.debug(f"transparent control: {name} {args}")
        self._forward(name, args)
