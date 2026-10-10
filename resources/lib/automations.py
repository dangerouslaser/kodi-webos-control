"""Playback rules: pick the TV picture preset from Kodi's HDR type.

Runs on the service loop. The Kodi thread forwards player events with
``loop.call_soon_threadsafe(rules.av_started, hdr_type)`` / ``rules.stopped``.

Restore semantics: webOS keeps separate picture-mode memories for SDR and
HDR signals. The preset to restore after playback is the one seen while
Kodi was idle (the SDR GUI), not whatever the TV reported at the moment
playback began, which may already be an HDR memory.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from . import kodilog as log
from .errors import ActionError

if TYPE_CHECKING:
    from .settings import Settings
    from .tv import TvController

HDR_KINDS = ("sdr", "hdr10", "hlg", "dolbyvision")


def normalize_hdr_type(label: str) -> str | None:
    """Map VideoPlayer.HdrType to a rule key; None if unrecognized."""
    value = (label or "").strip().lower().replace(" ", "").replace("+", "plus")
    if value in ("", "sdr", "none"):
        return "sdr"
    if value in ("hdr10", "hdr10plus", "hdr"):
        return "hdr10"
    if value in ("dolbyvision", "dv"):
        return "dolbyvision"
    if value == "hlg":
        return "hlg"
    return None


class PlaybackRules:
    def __init__(self, controller: TvController, settings: Settings) -> None:
        self.controller = controller
        self.settings = settings
        self.idle_mode: str | None = controller.picture_preset or None
        self.applied = False
        self._session = False
        self._pending: asyncio.Task | None = None
        controller.picture_listeners.append(self._on_picture)

    def update(self, settings: Settings) -> None:
        self.settings = settings

    def presets(self) -> dict[str, str]:
        s = self.settings
        return {
            "sdr": s.preset_sdr,
            "hdr10": s.preset_hdr10,
            "hlg": s.preset_hlg,
            "dolbyvision": s.preset_dv,
        }

    def _on_picture(self, mode: str) -> None:
        # Only learn the "home" preset while nothing is playing.
        if not self._session:
            self.idle_mode = mode

    def _schedule(self, coro_factory) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        self._pending = asyncio.ensure_future(self._delayed(coro_factory))

    async def _delayed(self, coro_factory) -> None:
        await asyncio.sleep(max(0.0, self.settings.rules_debounce))
        await coro_factory()

    # events (loop thread) -------------------------------------------------

    def av_started(self, hdr_label: str) -> None:
        self._session = True
        kind = normalize_hdr_type(hdr_label)
        log.debug(f"video started, HdrType={hdr_label!r} -> {kind}")
        if kind is None:
            log.info(f"unknown HdrType {hdr_label!r}; no picture rule applied")
            self._schedule(_noop)
            return
        self._schedule(lambda: self._apply(kind))

    def stopped(self) -> None:
        if not self._session:
            return
        log.debug("playback stopped")
        self._schedule(self._restore)

    # actions --------------------------------------------------------------

    async def _apply(self, kind: str) -> None:
        if not self.settings.rules_enabled:
            return
        target = self.presets().get(kind, "")
        if not target or not self.controller.connected:
            return
        if self.controller.picture_preset == target:
            return
        try:
            await self.controller.set_picture_mode(target)
        except ActionError as err:
            log.warning(f"{kind} rule: cannot set preset {target!r}: {err.message}")
            return
        self.applied = True
        log.info(f"{kind} playback: picture preset {target}")

    async def _restore(self) -> None:
        try:
            if not (self.settings.rules_enabled and self.settings.restore_on_stop and self.applied):
                return
            target = self.idle_mode
            if not target or not self.controller.connected:
                return
            if self.controller.picture_preset != target:
                try:
                    await self.controller.set_picture_mode(target)
                    log.info(f"playback ended: restored picture preset {target}")
                except ActionError as err:
                    log.warning(f"cannot restore preset {target!r}: {err.message}")
        finally:
            self.applied = False
            self._session = False

    async def close(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pending
        with contextlib.suppress(ValueError):
            self.controller.picture_listeners.remove(self._on_picture)


async def _noop() -> None:
    return None
