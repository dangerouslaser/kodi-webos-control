"""TV watcher: react in Kodi to what happens on the TV.

- TV turned off: pause, stop, or shut down (settings.on_tv_off).
- TV switched away from Kodi's input: pause or stop, optionally resume on
  return (settings.on_switch_away, settings.resume_on_return).
- Transparent control: while the TV shows something other than Kodi, an
  invisible Kodi dialog catches remote presses and forwards them to the TV.

Runs on the service loop and is fed every TvState the controller
publishes. Kodi-side effects go through a KodiSide object, so tests can
run without Kodi.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from typing import TYPE_CHECKING, Protocol

from . import kodilog as log

if TYPE_CHECKING:
    from .settings import Settings
    from .state import TvState
    from .tv import TvController

# A dropped connection only counts as "TV off" if it stays down this long.
DROP_GRACE = 15.0
# Wait before shutting down, in case the TV comes straight back on.
SHUTDOWN_DELAY = 20.0
# Input changes must hold this long (webOS reports transient apps while
# switching).
FOREGROUND_SETTLE = 1.0
KODI_APP_PREFIX = "org.xbmc.kodi"


class KodiSide(Protocol):
    def is_playing(self) -> bool: ...  # playing and not paused
    def is_paused(self) -> bool: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def shutdown(self) -> None: ...
    def set_overlay(self, active: bool) -> None: ...
    def save_setting(self, key: str, value: str) -> None: ...


def kodi_apps(settings: Settings, state: TvState) -> set[str]:
    """webOS app ids that mean "Kodi is on screen"."""
    wanted = settings.kodi_input.casefold()
    if wanted:
        for input_id, label, app_id in state.inputs:
            if wanted in (input_id.casefold(), label.casefold(), app_id.casefold()):
                return {app_id}
        return {settings.kodi_input}
    if settings.is_loopback:
        # Kodi running on the TV itself.
        apps = {a for a, _t in state.apps if a.startswith(KODI_APP_PREFIX)}
        return apps or {KODI_APP_PREFIX}
    return set()


def kodi_foreground(settings: Settings, state: TvState) -> bool | None:
    """True/False when Kodi is/isn't on screen; None when unknown or TV off."""
    if not (state.connected and state.is_on and state.app_id):
        return None
    apps = kodi_apps(settings, state)
    if not apps:
        return None
    return state.app_id in apps


class TvWatcher:
    def __init__(self, controller: TvController, settings: Settings, kodi: KodiSide) -> None:
        self.controller = controller
        self.settings = settings
        self.kodi = kodi
        self._was_on = False
        self._off_task: asyncio.Task | None = None
        self._shutdown_task: asyncio.Task | None = None
        self._foreground: bool | None = None
        self._fg_task: asyncio.Task | None = None
        self._paused_by_us = False
        self._overlay = False
        controller.state_listeners.append(self._on_state)

    def update(self, settings: Settings) -> None:
        self.settings = settings
        # Kodi's input may have changed: re-evaluate what's on screen.
        self._on_state(self.controller.state)
        self._update_overlay()

    # state ----------------------------------------------------------------

    def _on_state(self, state: TvState) -> None:
        on = state.connected and state.is_on
        if on:
            self._was_on = True
            _cancel(self._off_task)
            if self._shutdown_task is not None and not self._shutdown_task.done():
                log.info("TV is back on; shutdown cancelled")
                _cancel(self._shutdown_task)
        elif self._was_on:
            if state.connected:
                # The TV reported standby/off itself.
                _cancel(self._off_task)
                self._tv_turned_off()
            elif self._off_task is None or self._off_task.done():
                self._off_task = asyncio.ensure_future(self._off_after_drop())

        foreground = kodi_foreground(self.settings, state)
        if foreground is None and on and not state.app_id:
            return  # transient: switching apps
        if foreground != self._foreground:
            self._foreground = foreground
            _cancel(self._fg_task)
            self._fg_task = asyncio.ensure_future(self._settle_foreground(foreground))

    async def _off_after_drop(self) -> None:
        await asyncio.sleep(DROP_GRACE)
        if not self.controller.connected:
            log.info("TV connection lost and not back; treating the TV as off")
            self._tv_turned_off()

    def _tv_turned_off(self) -> None:
        self._was_on = False
        action = self.settings.on_tv_off
        log.info(f"TV turned off (action: {action})")
        if action == "pause" and self.kodi.is_playing():
            self.kodi.pause()
        elif action == "stop" and (self.kodi.is_playing() or self.kodi.is_paused()):
            self.kodi.stop()
        elif action == "shutdown":
            _cancel(self._shutdown_task)
            self._shutdown_task = asyncio.ensure_future(self._shutdown_later())

    async def _shutdown_later(self) -> None:
        await asyncio.sleep(SHUTDOWN_DELAY)
        if self.controller.connected and self.controller.state.is_on:
            return
        log.info("TV stayed off; shutting down")
        self.kodi.shutdown()

    async def _settle_foreground(self, foreground: bool | None) -> None:
        await asyncio.sleep(FOREGROUND_SETTLE)
        self._update_overlay()
        if foreground is False:
            self._switched_away()
        elif foreground is True:
            self._returned()

    def _switched_away(self) -> None:
        action = self.settings.on_switch_away
        if action == "none":
            return
        if self.kodi.is_playing():
            log.info(f"TV left Kodi's input; {action} playback")
            if action == "pause":
                self.kodi.pause()
                self._paused_by_us = True
            else:
                self.kodi.stop()
        elif action == "stop" and self.kodi.is_paused():
            self.kodi.stop()

    def _returned(self) -> None:
        paused_by_us, self._paused_by_us = self._paused_by_us, False
        if (
            paused_by_us
            and self.settings.on_switch_away == "pause"
            and self.settings.resume_on_return
            and self.kodi.is_paused()
        ):
            log.info("TV is back on Kodi's input; resuming playback")
            self.kodi.resume()

    def _update_overlay(self) -> None:
        active = self.settings.transparent_control and self._foreground is False
        if active != self._overlay:
            self._overlay = active
            log.info(f"transparent control {'on' if active else 'off'}")
            self.kodi.set_overlay(active)

    # playback -------------------------------------------------------------

    def video_started(self) -> None:
        """Learn Kodi's input the first time a video plays (off-TV mode)."""
        self.learn_input("video started")

    def learn_input(self, reason: str) -> None:
        """If Kodi's input isn't set, take the TV's current input.

        Called when Kodi must be on screen: a video just started, or the
        user just changed settings in Kodi's UI.
        """
        if self.settings.kodi_input or self.settings.is_loopback:
            return
        state = self.controller.state
        if not (state.connected and state.is_on and state.input_id):
            return
        log.info(f"learned Kodi's TV input ({reason}): {state.input_id} ({state.input_label})")
        self.settings = dataclasses.replace(self.settings, kodi_input=state.input_id)
        self.kodi.save_setting("kodi_input", state.input_id)
        self._on_state(state)

    def status(self) -> dict:
        return {
            "kodi_input": self.settings.kodi_input,
            "kodi_on_screen": self._foreground,
            "transparent_control": self.settings.transparent_control,
            "transparent_active": self._overlay,
            "on_tv_off": self.settings.on_tv_off,
            "on_switch_away": self.settings.on_switch_away,
        }

    async def close(self) -> None:
        for task in (self._off_task, self._shutdown_task, self._fg_task):
            _cancel(task)
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._overlay:
            self._overlay = False
            self.kodi.set_overlay(False)
        with contextlib.suppress(ValueError):
            self.controller.state_listeners.remove(self._on_state)


def _cancel(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()
