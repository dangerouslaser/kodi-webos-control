"""WebOsService: asyncio loop thread, Kodi Monitor, IPC dispatch.

The Kodi thread never does network I/O. Monitor callbacks hand work to
the loop with run_coroutine_threadsafe; the loop thread owns TvController.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import json
import os
import sys
import threading
from concurrent.futures import Future
from typing import Any

import xbmc
import xbmcaddon
import xbmcgui

from . import bootstrap, ipc
from . import kodilog as log
from .errors import ActionError, BadRequest
from .settings import Settings

ADDON_ID = "service.webostv"
ADDON_NAME = "LG webOS TV"
SHUTDOWN_TIMEOUT = 1.0
SHUTDOWN_BUDGET = 3.0
# A shutdown still running this long (Kodi kills at 5 s) dumps its threads here.
STALL_AFTER = 4.0
STALL_FILE = "shutdown-stall.txt"
LOCK_FILE = "service.lock"
# Home menu under the Remote Bridge pointer (see FocusKeeper).
HOME_VIEW = "WebOS.HomeView"
POINTER_CLICK = "WebOS.PointerClick"
CLICK_FLAG_TIME = 1.0
# Estuary LG's side menu rows: list top and row height (Home.xml, 9000).
MENU_TOP, MENU_ROW = 40, 95
POINTER_IDLE = 5.0  # Kodi hides its mouse after 5 s without movement
# Settings side menus under the pointer (see FocusKeeper): the skin's pad over
# the category column (SettingsCategory.xml, the LG TV page), its rows, and
# the first category button (Kodi numbers its generated ones from -200).
POINTING = "WebOS.Pointing"
PAD_ROW = "WebOS.PadRow"
SETTINGS_PAD = 9101
PAD_TOP, PAD_ROW_HEIGHT = 130, 85
LGTV_WINDOW, LGTV_FIRST_CATEGORY = 1120, 21
KODI_FIRST_CATEGORY = -200
# Where things are on screen (FocusKeeper.places), kept between restarts.
PLACES_FILE = "pointer_places.json"
# Remote Bridge wheel -> (gamepad trigger mapped to ScrollUp/ScrollDown in
# gamepad.xml, remote button outside lists, remote button in a horizontal row).
WHEEL = {"wheelup": ("leftanalogtrigger", "up", "left"),
         "wheeldown": ("rightanalogtrigger", "down", "right")}
# Kodi's lists move one item once the squared analog amount passes 0.4; the
# trigger arrives in 1/255 steps, and 162/255 squared is just over it.
SCROLL_AMOUNT = 162.4 / 255


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


class _Player(xbmc.Player):
    """Forwards playback events to the playback and sound rules on the loop."""

    def __init__(self, service: WebOsService) -> None:
        super().__init__()
        self._service = service

    def onAVStarted(self) -> None:  # noqa: N802
        if self.isPlayingVideo():
            content = "livetv" if xbmc.getCondVisibility("PVR.IsPlayingTV") else "video"
            hdr = xbmc.getInfoLabel("VideoPlayer.HdrType")
            self._service.player_event("av_started", content, hdr, _video_fps(), _video_height())
        elif self.isPlayingAudio():
            self._service.player_event("av_started", "music")

    def onPlayBackStopped(self) -> None:  # noqa: N802
        self._service.player_event("stopped")

    def onPlayBackEnded(self) -> None:  # noqa: N802
        self._service.player_event("ended")

    def onPlayBackError(self) -> None:  # noqa: N802
        self._service.player_event("stopped")


def _video_fps() -> float:
    try:
        return float(xbmc.getInfoLabel("Player.Process(videofps)") or 0)
    except ValueError:
        return 0.0


def _video_height() -> int:
    try:
        return int(float(xbmc.getInfoLabel("Player.Process(videoheight)") or 0))
    except ValueError:
        return 0


def _press_button(name: str) -> None:
    """A button of Kodi's remote keymap, so it means what it does in the current window."""
    xbmc.executeJSONRPC(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "Input.ButtonEvent",
        "params": {"button": name, "keymap": "R1"},
    }))


class FocusKeeper:
    """What the Remote Bridge pointer does to focus (loop thread).

    Over empty space Kodi unfocuses everything and forgets the control
    (System.CurrentControlID goes empty); the next action would then go to
    the window's default control (on Estuary's home screen: the side menu,
    which opens). So remember the control and its item while the pointer
    moves, and put focus back there when it comes to rest.

    Home menu (Estuary LG): the home screen shows the side menu's selected
    section, and hovering moves the selection. While the pointer is in use,
    the section on show is frozen in WebOS.HomeView, so hovering only
    highlights; a pointer click on a menu item switches to it (and
    WebOS.PointerClick tells the skin not to open the library). The next
    button press, or the pointer idling until Kodi hides it, puts the menu
    selection back on that section and clears the property: buttons behave
    as before.

    Settings side menus: Kodi switches the category page as soon as a
    category gets focus, so hovering down the list flicked through them.
    While the pointer is in use (WebOS.Pointing), the skin covers the list
    with a pad (SETTINGS_PAD): hovering highlights the row under the pointer
    (WebOS.PadRow), a click focuses that category, and a button press hands
    focus back to the list.
    """

    def __init__(self) -> None:
        from .pointermap import PointerMap  # noqa: PLC0415

        from . import storage  # noqa: PLC0415

        self.places = PointerMap()  # where focused things are, from pointer positions
        # Learned per skin: another skin (or version) has another layout.
        self._skin = f"{xbmc.getSkinDir()} {xbmc.getInfoLabel('System.AddonVersion(' + xbmc.getSkinDir() + ')')}"
        saved = storage.read_json(PLACES_FILE, {})
        if isinstance(saved, dict) and saved.get("skin") == self._skin:
            self.places.load(saved)
        self._last: tuple[str, str, str] | None = None  # window, control, item
        self._view: tuple[str, str] | None = None  # frozen section: id, item (1-based)
        self._idle: asyncio.TimerHandle | None = None
        self._click_reset: asyncio.TimerHandle | None = None
        self._pointing = False
        self._pad_row: int | None = None
        self._y = -1.0

    # -- pointer focus --

    def track(self, x: float = -1, y: float = -1) -> None:
        """The pointer moving, at x, y in skin coordinates (1920x1080)."""
        self._pointer_used()
        self._y = y
        self._pad_hover(y)
        if not self._menu_row_under(y):
            self._menu_back_to_view()
        control = xbmc.getInfoLabel("System.CurrentControlID")
        if control and control != "0":
            window = xbmc.getInfoLabel("System.CurrentWindow")
            item = xbmc.getInfoLabel(f"Container({control}).CurrentItem")
            self._last = (window, control, item)
            if x >= 0 and y >= 0:
                self.places.note((window, control, _pointer_context()), _slot(control), x, y)

    def spot(self) -> tuple[float, float] | None:
        """Where the focused item is on screen (skin coordinates), as far as the
        pointer has shown; None if it hasn't been near."""
        control = xbmc.getInfoLabel("System.CurrentControlID")
        if not control or control == "0":
            return None
        window = xbmc.getInfoLabel("System.CurrentWindow")
        return self.places.where((window, control, _pointer_context()), _slot(control))

    def restore(self) -> None:
        control = xbmc.getInfoLabel("System.CurrentControlID")
        if control and control != "0":
            return  # something has focus
        if self._last is None or self._last[0] != xbmc.getInfoLabel("System.CurrentWindow"):
            return
        _window, control, item = self._last
        if item.isdigit() and int(item) > 0:
            xbmc.executebuiltin(f"SetFocus({control},{int(item) - 1},absolute)")
        else:
            xbmc.executebuiltin(f"SetFocus({control})")

    # -- home menu --

    def click(self) -> None:
        """A pointer click, before Kodi gets it."""
        self._pointer_used()
        self._pad_click()
        if self._view is None or not _menu_has_focus():
            return
        selection = _home_selection()
        if selection is None:
            return
        self._view = selection
        home = xbmcgui.Window(10000)
        home.setProperty(HOME_VIEW, selection[0])
        home.setProperty(POINTER_CLICK, "true")
        if self._click_reset is not None:
            self._click_reset.cancel()
        self._click_reset = asyncio.get_running_loop().call_later(
            CLICK_FLAG_TIME, lambda: xbmcgui.Window(10000).clearProperty(POINTER_CLICK))

    def key(self) -> None:
        """A button press, before Kodi gets it: back to button behaviour."""
        self._release()
        if self._pointing:
            self._pointing = False
            if xbmc.getCondVisibility(f"Control.HasFocus({SETTINGS_PAD})"):
                xbmc.executebuiltin("SetFocus(3)")  # the category on show
            home = xbmcgui.Window(10000)
            home.clearProperty(POINTING)
            home.clearProperty(PAD_ROW)
            self._pad_row = None

    # -- settings side menus --

    def _pad_hover(self, y: float) -> None:
        row = self._pad_row_at(y) if xbmc.getCondVisibility(f"Control.HasFocus({SETTINGS_PAD})") else None
        if row != self._pad_row:
            self._pad_row = row
            home = xbmcgui.Window(10000)
            if row is None:
                home.clearProperty(PAD_ROW)
            else:
                home.setProperty(PAD_ROW, str(row))

    def _pad_click(self) -> None:
        if not xbmc.getCondVisibility(f"Control.HasFocus({SETTINGS_PAD})"):
            return
        row = self._pad_row_at(self._y)
        if row is None:
            return
        first = LGTV_FIRST_CATEGORY if xbmc.getCondVisibility(
            f"Window.IsActive({LGTV_WINDOW})") else KODI_FIRST_CATEGORY
        xbmc.executebuiltin(f"SetFocus({first + row})")

    def _pad_row_at(self, y: float) -> int | None:
        count = xbmc.getInfoLabel("Container(3).NumItems")
        if y < PAD_TOP or not count.isdigit():
            return None
        row = int((y - PAD_TOP) // PAD_ROW_HEIGHT)
        return row if row < int(count) else None

    def close(self) -> None:
        """Service stopping: no GUI calls (see WebOsService.shutdown)."""
        from . import storage  # noqa: PLC0415

        for handle in (self._idle, self._click_reset):
            if handle is not None:
                handle.cancel()
        try:
            storage.write_json(PLACES_FILE, {"skin": self._skin, **self.places.dump()})
        except OSError as err:
            log.warning(f"cannot save pointer places: {err}")

    def _pointer_used(self) -> None:
        if not self._pointing:
            self._pointing = True
            xbmcgui.Window(10000).setProperty(POINTING, "true")
        if self._idle is not None:
            self._idle.cancel()
        self._idle = asyncio.get_running_loop().call_later(POINTER_IDLE, self._release)
        if self._view is None and xbmc.getCondVisibility("Window.IsActive(home)"):
            self._view = _home_selection()
            if self._view is not None:
                xbmcgui.Window(10000).setProperty(HOME_VIEW, self._view[0])

    def _menu_row_under(self, y: float) -> bool:
        """Over the open menu's label area (the hover pad, 9100), the row at height
        y gets the highlight, as if hovered: the menu's own hit area is its icons."""
        if self._view is None or y < 0 or not xbmc.getCondVisibility("Control.HasFocus(9100)"):
            return False
        current, position = (xbmc.getInfoLabel(f"Container(9000).{label}")
                             for label in ("CurrentItem", "Position"))
        count = xbmc.getInfoLabel("Container(9000).NumItems")
        if not (current.isdigit() and position.isdigit() and count.isdigit()):
            return False
        row = int((y - MENU_TOP) // MENU_ROW)
        target = int(current) - int(position) + row  # first row on screen + row
        if row < 0 or not 1 <= target <= int(count):
            return True  # in the sidebar, off the rows: leave the highlight
        if target != int(current):
            xbmc.executebuiltin(f"Control.Move(9000,{target - int(current)})")
        return True

    def _menu_back_to_view(self) -> None:
        """Pointer off the menu: its selection (the collapsed bar's highlight) back
        on the section on show, not the last one hovered."""
        if self._view is None or _menu_has_focus():
            return
        current = xbmc.getInfoLabel("Container(9000).CurrentItem")
        item = self._view[1]
        if item.isdigit() and current.isdigit() and item != current:
            xbmc.executebuiltin(f"Control.Move(9000,{int(item) - int(current)})")

    def _release(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        if self._view is None:
            return
        _section, item = self._view
        self._view = None
        current = xbmc.getInfoLabel("Container(9000).CurrentItem")
        if item.isdigit() and current.isdigit() and item != current:
            xbmc.executebuiltin(f"Control.Move(9000,{int(item) - int(current)})")
        xbmcgui.Window(10000).clearProperty(HOME_VIEW)


def _slot(control: str) -> tuple[int, int] | None:
    """The focused item's slot in the visible part of a container: (row, column)
    in a panel, (position, 0) in a list; None for a plain control."""
    row, column = (xbmc.getInfoLabel(f"Container({control}).{label}") for label in ("Row", "Column"))
    if row.isdigit() and column.isdigit():
        return int(row), int(column)
    position = xbmc.getInfoLabel(f"Container({control}).Position")
    if position.isdigit() and xbmc.getInfoLabel(f"Container({control}).NumItems"):
        return int(position), 0
    return None


def _pointer_context() -> str:
    """What moves controls as a whole, so they're remembered per arrangement: a
    skin that sets Window(Home).Property(WebOS.PointerContext) provides it as
    the variable WebOSPointerContext (Estuary LG: how far its home rows scrolled)."""
    if not xbmc.getInfoLabel("Window(Home).Property(WebOS.PointerContext)"):
        return ""
    return xbmc.getInfoLabel("$VAR[WebOSPointerContext]")


def _menu_has_focus() -> bool:
    """The side menu (9000) or the open sidebar's hover pad over its labels (9100)."""
    return any(xbmc.getCondVisibility(f"Control.HasFocus({control})") for control in (9000, 9100))


def _home_selection() -> tuple[str, str] | None:
    section = xbmc.getInfoLabel("Container(9000).ListItem.Property(id)")
    item = xbmc.getInfoLabel("Container(9000).CurrentItem")
    return (section, item) if section and item.isdigit() else None


def _at_row_end(control: str, direction: str) -> bool:
    item = xbmc.getInfoLabel(f"Container({control}).CurrentItem")
    count = xbmc.getInfoLabel(f"Container({control}).NumItems")
    if not (item.isdigit() and count.isdigit()):
        return False
    return int(item) <= 1 if direction == "left" else int(item) >= int(count)


def _skin_says(prop: str) -> bool:
    """A condition the skin publishes on Window(Home) (Estuary LG: which
    containers the wheel moves by button). Infolabels only: xbmcgui control
    access from this thread crashes Kodi."""
    condition = xbmc.getInfoLabel(f"Window(Home).Property({prop})")
    return bool(condition) and xbmc.getCondVisibility(condition)


def _eventserver_port() -> int:
    """Kodi's EventServer port (Settings > Services > Control)."""
    try:
        reply = json.loads(xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "Settings.GetSettingValue",
            "params": {"setting": "services.esport"},
        })))
        return int(reply["result"]["value"])
    except (KeyError, TypeError, ValueError):
        return 9777


def _log_task_error(task: Any) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning(f"background task failed: {task.exception()!r}")


class _KodiSide:
    """What the TV watcher may do to Kodi (called from the loop thread)."""

    def __init__(self, service: WebOsService) -> None:
        self._service = service

    def is_playing(self) -> bool:
        return xbmc.getCondVisibility("Player.Playing")

    def is_paused(self) -> bool:
        return xbmc.getCondVisibility("Player.Paused")

    def pause(self) -> None:
        if self.is_playing():
            xbmc.executebuiltin("PlayerControl(Play)")  # toggles

    def resume(self) -> None:
        if self.is_paused():
            xbmc.executebuiltin("PlayerControl(Play)")

    def stop(self) -> None:
        xbmc.executebuiltin("PlayerControl(Stop)")

    def shutdown(self) -> None:
        # Kodi's own shutdown function (Settings > System > Power saving).
        xbmc.executebuiltin("ShutDown()")

    def set_overlay(self, active: bool) -> None:
        if not self._service.closing:
            window = xbmcgui.Window(10000)
            if active:
                window.setProperty("WebOS.TransparentControl", "true")
            else:
                window.clearProperty("WebOS.TransparentControl")
        if self._service.overlay is not None:
            self._service.overlay.set_active(active)

    def save_setting(self, key: str, value: str) -> None:
        xbmcaddon.Addon(ADDON_ID).setSetting(key, value)


class WebOsService:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.controller: Any = None
        self.publisher: Any = None
        self.monitor: _Monitor | None = None
        self.player: _Player | None = None
        self.rules: Any = None
        self.sound: Any = None
        self.watcher: Any = None
        self.bridge: Any = None
        self.mouse: Any = None
        self.focus: Any = None
        self._inputs: tuple = ()
        self._launcher_key: tuple = ()
        self._lock_fd: int | None = None
        self.overlay: Any = None
        self.closing = False

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
        if not self._acquire_instance_lock():
            # Kodi can start a service twice when it's installed while Kodi
            # runs; two copies would fight over the TV and run every action twice.
            log.warning("another copy of the service is already running; this one exits")
            return False

        # Lazy imports: only pay for aiowebostv/websockets once we know we run.
        from .automations import PlaybackRules  # noqa: PLC0415
        from .bridge import RemoteBridge  # noqa: PLC0415
        from .eventserver import EventServerMouse  # noqa: PLC0415
        from .remote import Overlay  # noqa: PLC0415
        from .soundrules import SoundRules  # noqa: PLC0415
        from .state import PropertyPublisher  # noqa: PLC0415
        from .tv import TvController  # noqa: PLC0415
        from .watcher import TvWatcher  # noqa: PLC0415

        self.publisher = PropertyPublisher()
        # Pointer state (a frozen Home section, the settings pad) from when the
        # service last stopped.
        home = xbmcgui.Window(10000)
        for prop in (HOME_VIEW, POINTER_CLICK, POINTING, PAD_ROW):
            home.clearProperty(prop)
        self.overlay = Overlay(self.forward, lambda: self.settings.kodi_input)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._loop_main, name="webostv-loop", daemon=True)
        self.thread.start()

        async def create() -> None:
            # WebOsClient calls get_running_loop() in __init__, so the
            # controller (which builds clients) is created on the loop.
            self.controller = TvController(
                self.settings,
                on_state=self._on_state,
                on_mac=self.save_mac,
                on_host=self.save_host,
                notify=self.notify,
            )
            await self.controller.start()
            self.rules = PlaybackRules(self.controller, self.settings)
            self.sound = SoundRules(self.controller, self.settings)
            self.watcher = TvWatcher(self.controller, self.settings, _KodiSide(self))
            self.mouse = EventServerMouse(_eventserver_port())
            self.focus = FocusKeeper()
            self.bridge = RemoteBridge(self.settings, self._remote_button, self._kodi_on_screen,
                                       self._bridge_changed, self.mouse, self._tv_cursor,
                                       self.focus, self._tv_pointer_settings)
            await self.bridge.start()

        self._submit(create()).result(timeout=5)
        self.publish_setting_labels()
        self.monitor = _Monitor(self)
        self.player = _Player(self)
        log.info(
            f"service started (host {self.settings.host}, "
            f"Python {sys.version.split()[0]}, "
            f"Kodi {xbmc.getInfoLabel('System.BuildVersion') or 'unknown'})"
        )
        return True

    def _acquire_instance_lock(self) -> bool:
        try:
            import fcntl  # noqa: PLC0415
        except ImportError:  # Windows: no flock; rely on Kodi
            return True
        from . import storage  # noqa: PLC0415

        path = storage.data_path(LOCK_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def _release_instance_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing releases the flock
            self._lock_fd = None

    def _loop_main(self) -> None:
        assert self.loop is not None
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def shutdown(self) -> None:
        """Stop without touching Kodi's GUI.

        Kodi stops a service (disable, update) while holding its add-on
        manager lock, and the skin's System.HasAddon/Addon.Setting* checks
        need that lock while the GUI lock is held. Window property writes
        take the GUI lock, so one here can deadlock Kodi for good. The
        WebOS.* properties stay until the next start overwrites them.
        """
        log.info("service stopping")
        self.closing = True
        self._watch_shutdown()
        if self.publisher is not None:
            self.publisher.close()
        if self.loop is None:
            return
        if self.focus is not None:
            self.loop.call_soon_threadsafe(self.focus.close)
        if self.overlay is not None:
            self.overlay.close(wait=False)
        # Kodi kills a service that takes 5 s to stop (and then never restarts
        # it), so everything stops together within SHUTDOWN_BUDGET.
        try:
            self._submit(self._stop_parts()).result(timeout=SHUTDOWN_BUDGET)
        except Exception as err:  # noqa: BLE001
            log.warning(f"service parts did not stop cleanly: {err!r}")
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(0.5)
        self._release_instance_lock()
        self._unwatch_shutdown()
        self.monitor = None
        self.player = None
        self.loop = None

    def _watch_shutdown(self) -> None:
        """If stopping nears Kodi's 5 s limit, save every thread's stack to
        shutdown-stall.txt (add-on data) to show what it's waiting on."""
        from . import storage  # noqa: PLC0415

        try:
            self._stall_file = open(storage.data_path(STALL_FILE), "w")  # noqa: SIM115
            faulthandler.dump_traceback_later(STALL_AFTER, file=self._stall_file)
        except (OSError, ValueError, RuntimeError) as err:
            log.warning(f"cannot watch the shutdown: {err!r}")

    def _unwatch_shutdown(self) -> None:
        """Stopped in time: nothing to report."""
        faulthandler.cancel_dump_traceback_later()
        stall = getattr(self, "_stall_file", None)
        if stall is not None:
            stall.close()
            with contextlib.suppress(OSError):
                os.remove(stall.name)

    async def _stop_parts(self) -> None:
        """Loop thread: stop the parts, then the TV connection, each bounded."""
        parts = [p for p in (self.bridge, self.watcher, self.rules, self.sound) if p is not None]
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(*(p.close() for p in parts), return_exceptions=True),
                                   SHUTDOWN_TIMEOUT)
        if self.controller is not None:
            try:
                await asyncio.wait_for(self.controller.stop(), SHUTDOWN_TIMEOUT)
            except Exception as err:  # noqa: BLE001
                log.warning(f"controller stop did not finish cleanly: {err!r}")
        # getaddrinfo runs in the default executor; its worker thread is not
        # a daemon, and Kodi waits for it (then kills the script after 5 s).
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.get_running_loop().shutdown_default_executor(), 0.5)

    def _submit(self, coro: Any) -> Future:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # Kodi callbacks (Kodi thread) ---------------------------------------

    def _on_state(self, state: Any) -> None:
        """Loop thread: every TvState the controller publishes."""
        self.publisher.publish(state)
        if self.bridge is not None:
            # Take the remote over or hand it back as Kodi comes and goes.
            self.bridge.refresh()
        # Refresh launcher.json (plugin listings) when apps or inputs change.
        key = (tuple(a for a, _t in state.apps), tuple(i[0] for i in state.inputs))
        if state.connected and state.apps and key != self._launcher_key:
            self._launcher_key = key
            from . import launcher  # noqa: PLC0415

            try:
                points = self.controller.launch_points()
            except ActionError:
                return
            task = asyncio.ensure_future(launcher.refresh(points))
            task.add_done_callback(_log_task_error)
        # Input names arrive after connecting; refresh WebOS.KodiInput.
        if state.inputs and state.inputs != self._inputs:
            self._inputs = state.inputs
            self.publish_setting_labels()

    def publish_setting_labels(self) -> None:
        """WebOS.Rule.<kind>: friendly name of each HDR rule preset (skins)."""
        from .presets import label  # noqa: PLC0415

        if self.closing:
            return

        window = xbmcgui.Window(10000)
        s = self.settings
        # Kodi's input, with the TV's name for it when known.
        kodi_input = s.kodi_input
        state = getattr(self.controller, "state", None)
        for input_id, input_label, _app in getattr(state, "inputs", ()):
            if input_id == kodi_input and input_label != input_id:
                kodi_input = f"{input_label} ({input_id})"
        for key, value in (("KodiInput", kodi_input), ("ConfiguredHost", s.host)):
            if value:
                window.setProperty(f"WebOS.{key}", value)
            else:
                window.clearProperty(f"WebOS.{key}")
        for kind, value in (("SDR", s.preset_sdr), ("HDR10", s.preset_hdr10),
                            ("HLG", s.preset_hlg), ("DolbyVision", s.preset_dv)):
            if value:
                window.setProperty(f"WebOS.Rule.{kind}", label(value))
            else:
                window.clearProperty(f"WebOS.Rule.{kind}")

    def reload_settings(self) -> None:
        settings = Settings.load()
        log.set_debug(settings.debug)
        if settings == self.settings:
            return
        self.settings = settings
        self.publish_setting_labels()
        if self.loop is not None:
            for part in (self.rules, self.sound, self.watcher, self.bridge):
                if part is not None:
                    self.loop.call_soon_threadsafe(part.update, settings)
            # Someone is changing settings in Kodi's UI, so the TV is
            # showing Kodi right now: a good moment to learn its input.
            if self.watcher is not None:
                self.loop.call_soon_threadsafe(self.watcher.learn_input, "settings changed")
        if self.controller is not None:
            self._submit(self.controller.reconfigure(settings))

    def player_event(self, event: str, content: str = "", hdr: str = "", fps: float = 0.0,
                     height: int = 0) -> None:
        if self.loop is None or self.rules is None:
            return
        call = self.loop.call_soon_threadsafe
        if event == "av_started":
            if content != "music":
                call(self.rules.av_started, hdr, fps, content, height)
                if self.watcher is not None:
                    call(self.watcher.video_started)
            if self.sound is not None:
                call(self.sound.started, content)
            return
        call(getattr(self.rules, event))
        if self.sound is not None:
            call(self.sound.stopped)

    def _bridge_changed(self, state: str) -> None:
        """Loop thread: WebOS.RemoteBridge for skins (connected, waiting, or empty)."""
        if self.closing:
            return
        window = xbmcgui.Window(10000)
        if state:
            window.setProperty("WebOS.RemoteBridge", state)
        else:
            window.clearProperty("WebOS.RemoteBridge")

    def _tv_cursor(self, images: dict[str, bytes], hotspot: str) -> None:
        """Loop thread: the TV's own pointer images, for the skin's Pointer.xml.

        Kodi loads pointer textures when the skin loads, so new or changed
        images (first connection, a firmware update) need a skin reload;
        never during playback, where they wait for the next one.
        """
        from . import cursor, storage  # noqa: PLC0415

        async def install() -> None:
            assert self.loop is not None
            paths, changed = await self.loop.run_in_executor(
                None, cursor.install, images, storage.data_path("pointer"), hotspot)
            log.info(f"remote bridge: using the TV's own pointer ({', '.join(sorted(paths))})"
                     + (", new images" if changed else ""))
            if changed and not self.closing and not xbmc.getCondVisibility("Player.HasMedia"):
                xbmc.executebuiltin("ReloadSkin()")

        task = asyncio.ensure_future(install())
        task.add_done_callback(_log_task_error)

    def _tv_pointer_settings(self, tv: dict[str, str]) -> None:
        """Loop thread: the TV reported its pointer settings; mirror them in ours
        (the TV is the source of truth; a change here is sent there)."""
        from .settings import tv_pointer_to_addon  # noqa: PLC0415

        if self.closing:
            return
        addon = xbmcaddon.Addon(ADDON_ID)
        for key, value in tv_pointer_to_addon(tv).items():
            if addon.getSetting(key) != value:
                addon.setSetting(key, value)

    def _remote_button(self, name: str) -> None:
        """Loop thread: a Remote Bridge button; the wheel scrolls what has focus."""
        if name == "kodihome":  # the remote's Home (held: the TV's home)
            xbmc.executebuiltin("ActivateWindow(home)")
            return
        wheel = WHEEL.get(name)
        if wheel is None:
            _press_button(name)
            return
        trigger, button, sideways = wheel
        control = xbmc.getInfoLabel("System.CurrentControlID")
        if _skin_says("WebOS.WheelSideways"):
            # A horizontal row (the skin says which): one item per notch, stopping
            # at its ends (left from the first item would leave the row).
            if not _at_row_end(control, sideways):
                _press_button(sideways)
        elif _skin_says("WebOS.WheelButtons"):
            # A grid panel, whose analog scroll needs several notches per row.
            _press_button(button)
        elif control and xbmc.getInfoLabel(f"Container({control}).NumItems") and self.mouse:
            # A vertical list: Kodi's analog scroll, one item per notch.
            self.mouse.analog(trigger, SCROLL_AMOUNT)
        else:
            _press_button(button)

    def _kodi_on_screen(self) -> bool:
        """Loop thread: True only when the TV is known to be showing Kodi."""
        from .watcher import kodi_foreground  # noqa: PLC0415

        if self.controller is None:
            return False
        return kodi_foreground(self.settings, self.controller.state) is True

    def forward(self, name: str, args: list[Any]) -> None:
        """Run an action for transparent control; no reply, errors logged."""
        if self.loop is None or self.controller is None:
            return
        from . import actions  # noqa: PLC0415

        async def run() -> None:
            try:
                await actions.dispatch(self.controller, name, args)
            except ActionError as err:
                log.debug(f"forwarded {name} failed: {err.message}")

        self._submit(run())

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
            result = await actions.dispatch(self.controller, name, args, rules=self.rules)
            if name == "status" and self.watcher is not None:
                result = {**result, **self.watcher.status()}
            if name == "status" and self.bridge is not None:
                result = {**result, **self.bridge.status()}
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

    def save_mac(self, mac: str) -> None:
        """Persist a learned MAC (triggers onSettingsChanged -> reload)."""
        xbmcaddon.Addon(ADDON_ID).setSetting("mac", mac)

    def save_host(self, host: str) -> None:
        """Persist a host found by discovery (no reconnect: already there)."""
        xbmcaddon.Addon(ADDON_ID).setSetting("host", host)

    def notify(self, kind: str, message: str) -> None:
        icon = xbmcgui.NOTIFICATION_ERROR if kind == "error" else xbmcgui.NOTIFICATION_INFO
        xbmcgui.Dialog().notification(ADDON_NAME, message, icon, 5000, False)
