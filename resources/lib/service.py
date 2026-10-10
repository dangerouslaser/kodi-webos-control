"""WebOsService: asyncio loop thread, Kodi Monitor, IPC dispatch.

The Kodi thread never does network I/O. Monitor callbacks hand work to
the loop with run_coroutine_threadsafe; the loop thread owns TvController.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from concurrent.futures import Future
from typing import Any

import xbmc
import xbmcgui

from . import bootstrap, ipc
from . import kodilog as log
from .errors import ActionError, BadRequest
from .settings import Settings

ADDON_NAME = "LG webOS TV"
SHUTDOWN_TIMEOUT = 2.0


class _Monitor(xbmc.Monitor):
    def __init__(self, service: WebOsService) -> None:
        super().__init__()
        self._service = service

    def onNotification(self, sender: str, method: str, data: str) -> None:  # noqa: N802
        parsed = ipc.parse_notification(sender, method, data)
        if parsed is None or parsed[0] != ipc.REQUEST:
            return
        self._service.handle_request(parsed[1])

    def onSettingsChanged(self) -> None:  # noqa: N802
        self._service.reload_settings()


class WebOsService:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.controller: Any = None
        self.publisher: Any = None
        self.monitor: _Monitor | None = None

    # entry ---------------------------------------------------------------

    def run(self) -> None:
        """Start, block until Kodi asks us to abort, then shut down."""
        if not self.start():
            return
        assert self.monitor is not None
        self.monitor.waitForAbort()
        self.shutdown()

    def start(self) -> bool:
        if not bootstrap.python_supported():
            version = ".".join(map(str, sys.version_info[:3]))
            log.error(f"Python {version} is too old; aiowebostv needs 3.11+. Service disabled.")
            self.notify("error", f"Needs Python 3.11+ (found {version})")
            return False

        bootstrap.install()
        log.set_debug(self.settings.debug)
        log.install_handler()

        # Lazy imports: only pay for aiowebostv/websockets once we know we run.
        from .state import PropertyPublisher  # noqa: PLC0415
        from .tv import TvController  # noqa: PLC0415

        self.publisher = PropertyPublisher()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._loop_main, name="webostv-loop", daemon=True)
        self.thread.start()

        async def create() -> None:
            # WebOsClient calls get_running_loop() in __init__, so the
            # controller (which builds clients) is created on the loop.
            self.controller = TvController(
                self.settings, on_state=self.publisher.publish, notify=self.notify
            )
            await self.controller.start()

        self._submit(create()).result(timeout=5)
        self.monitor = _Monitor(self)
        log.info(
            f"service started (host {self.settings.host}, "
            f"Python {sys.version.split()[0]}, "
            f"Kodi {xbmc.getInfoLabel('System.BuildVersion') or 'unknown'})"
        )
        return True

    def _loop_main(self) -> None:
        assert self.loop is not None
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def shutdown(self) -> None:
        log.info("service stopping")
        if self.loop is None:
            return
        if self.controller is not None:
            try:
                self._submit(self.controller.stop()).result(timeout=SHUTDOWN_TIMEOUT)
            except Exception as err:  # noqa: BLE001
                log.warning(f"controller stop did not finish cleanly: {err!r}")
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(SHUTDOWN_TIMEOUT)
        if self.publisher is not None:
            self.publisher.clear()
        self.monitor = None
        self.loop = None

    def _submit(self, coro: Any) -> Future:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # Kodi callbacks (Kodi thread) ---------------------------------------

    def reload_settings(self) -> None:
        settings = Settings.load()
        log.set_debug(settings.debug)
        if settings == self.settings:
            return
        self.settings = settings
        if self.controller is not None:
            self._submit(self.controller.reconfigure(settings))

    def handle_request(self, request: dict[str, Any]) -> Future | None:
        if self.loop is None or self.controller is None:
            return None
        return self._submit(self._run_request(request))

    # loop thread ---------------------------------------------------------

    async def _run_request(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import actions  # noqa: PLC0415

        request_id = str(request.get("id"))
        name = str(request.get("action", ""))
        args = request.get("args") or []
        if not isinstance(args, list):
            args = [args]
        log.debug(f"request {name} {args}")
        try:
            result = await actions.dispatch(self.controller, name, args)
            reply = ipc.reply_ok(request_id, result)
        except ActionError as err:
            level = log.info if isinstance(err, BadRequest) else log.warning
            level(f"{name} failed: {err.code}: {err.message}")
            reply = ipc.reply_error(request_id, err.code, err.message)
        except Exception as err:  # noqa: BLE001
            log.error(f"{name} crashed: {err!r}")
            reply = ipc.reply_error(request_id, "internal_error", repr(err))
        if not reply["ok"] and request.get("notify_errors"):
            self.notify("error", f"{name}: {reply['message']}")
        ipc.notify_all(ipc.REPLY, reply)
        return reply

    def notify(self, kind: str, message: str) -> None:
        icon = xbmcgui.NOTIFICATION_ERROR if kind == "error" else xbmcgui.NOTIFICATION_INFO
        xbmcgui.Dialog().notification(ADDON_NAME, message, icon, 5000, False)
