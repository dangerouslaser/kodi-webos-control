"""Sound output rules: pick the TV's sound output from what Kodi plays.

Runs on the service loop. The Kodi thread forwards ``started(content)``
("video", "livetv" or "music") and ``stopped()``.

The output in use before Kodi changed it comes back when playback stops,
unless someone picked another output in the meantime. The restore waits a
few seconds, so the next playlist item (or a quick stop/start) doesn't
switch the output back and forth.
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

CONTENT_KINDS = ("video", "livetv", "music")
RESTORE_DELAY = 3.0


class SoundRules:
    def __init__(self, controller: TvController, settings: Settings) -> None:
        self.controller = controller
        self.settings = settings
        # Output before Kodi changed it, and the one Kodi set.
        self._previous: str | None = None
        self._set: str | None = None
        self._pending: asyncio.Task | None = None
        self._busy = False

    def update(self, settings: Settings) -> None:
        self.settings = settings

    def target(self, content: str) -> str:
        s = self.settings
        return {"video": s.sound_video, "livetv": s.sound_livetv, "music": s.sound_music}.get(content, "")

    # events (loop thread) -------------------------------------------------

    def started(self, content: str) -> None:
        log.debug(f"playback started: {content}")
        self._schedule(lambda: self._apply(content), 0.0)

    def stopped(self) -> None:
        if self._previous is None:
            return
        self._schedule(self._restore, max(RESTORE_DELAY, self.settings.rules_debounce))

    # actions --------------------------------------------------------------

    def _schedule(self, coro_factory, delay: float) -> None:
        previous = self._pending
        if previous is not None and not previous.done() and not self._busy:
            previous.cancel()  # still waiting: replace it
            previous = None
        self._pending = asyncio.ensure_future(self._delayed(coro_factory, delay, previous))

    async def _delayed(self, coro_factory, delay: float, previous) -> None:
        if previous is not None:
            with contextlib.suppress(Exception):
                await previous
        await asyncio.sleep(delay)
        self._busy = True
        try:
            await coro_factory()
        except ActionError as err:
            log.warning(f"sound output rule failed: {err.message}")
        finally:
            self._busy = False

    async def _apply(self, content: str) -> None:
        target = self.target(content)
        if not self.controller.connected:
            return
        if not target:
            # e.g. music switched the output, then a video with no rule plays.
            await self._restore()
            return
        current = self.controller.state.sound_output
        if self._previous is None:
            self._previous = current
        if current != target:
            await self.controller.sound_output(target)
            self._set = target
            log.info(f"{content} playback: sound output {target}")

    async def _restore(self) -> None:
        previous, self._previous = self._previous, None
        changed, self._set = self._set, None
        if not (previous and changed and self.controller.connected):
            return
        current = self.controller.state.sound_output
        if current != changed:
            log.info(f"sound output changed to {current} during playback; leaving it")
            return
        if previous != current:
            await self.controller.sound_output(previous)
            log.info(f"playback ended: sound output back to {previous}")

    async def close(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pending
