"""TvController: the only code that touches aiowebostv's WebOsClient.

Owns connection lifecycle (pairing, key storage, loopback fallback,
reconnect with backoff) and exposes the commands actions call. Everything
here runs on the service's asyncio loop thread.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

import aiohttp
from aiowebostv import WebOsClient, WebOsTvCommandError, WebOsTvPairError
from aiowebostv.buttons import BUTTONS

from . import kodilog as log
from . import storage, wol
from .errors import BadRequest, CommandFailed, Disconnected
from .net import Backoff, discover_lan_ip
from .pairing import KeyStore
from .settings import Settings
from .state import TvState

RUNTIME_FILE = "runtime.json"
CONNECTION_ERRORS = (OSError, TimeoutError, aiohttp.ClientError, WebOsTvCommandError)
MEDIA_COMMANDS = {
    "play": "play",
    "pause": "pause",
    "stop": "stop",
    "rewind": "rewind",
    "fast_forward": "fast_forward",
    "fastforward": "fast_forward",
}


class TvController:
    """Connection owner and command surface for one webOS TV."""

    def __init__(
        self,
        settings: Settings,
        *,
        keystore: KeyStore | None = None,
        on_state: Callable[[TvState], None] | None = None,
        notify: Callable[[str, str], None] | None = None,
        client_factory: Callable[..., WebOsClient] = WebOsClient,
        lan_ip: Callable[[], str | None] = discover_lan_ip,
        backoff_base: float = 2.0,
    ) -> None:
        self.settings = settings
        self.keystore = keystore or KeyStore()
        self._on_state = on_state or (lambda _s: None)
        # notify(kind, message): kind is "info" or "error"; the service maps
        # it to a Kodi notification.
        self._notify = notify or (lambda _k, _m: None)
        self._client_factory = client_factory
        self._lan_ip = lan_ip
        self._backoff = Backoff(base=backoff_base, cap=settings.backoff_cap)
        self.client: WebOsClient | None = None
        self.active_host = settings.host
        self.state = TvState(host=settings.host)
        self.pairing_blocked = False
        self._connected = False
        self._stopping = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._failure_notified = False
        self._had_key = False
        self._firmware: tuple[WebOsClient, str] | None = None
        self.picture_preset = ""

    # lifecycle -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected and self.client is not None and self.client.connection is not None

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="webostv-connection")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self._disconnect_client()
        self._set_connected(False)

    async def reconfigure(self, settings: Settings) -> None:
        """Apply new settings; reconnect if the target changed."""
        old = self.settings
        self.settings = settings
        self._backoff.cap = settings.backoff_cap
        if settings.host != old.host or settings.loopback_fallback != old.loopback_fallback:
            log.info(f"host changed {old.host} -> {settings.host}; reconnecting")
            storage.write_json(RUNTIME_FILE, {})
            self.pairing_blocked = False
            await self._disconnect_client()
            self._backoff.reset()
            self._wake.set()

    async def repair(self) -> None:
        """Forget the stored key and reconnect (shows the TV prompt once)."""
        log.info("re-pair requested; deleting stored client key")
        self.keystore.delete(self.settings.host)
        self.pairing_blocked = False
        self._failure_notified = False
        await self._disconnect_client()
        self._backoff.reset()
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopping:
            if self.pairing_blocked:
                await self._wait_wake(None)
                continue
            if await self._connect_cycle():
                self._backoff.reset()
                await self._wait_disconnected()
                if self._stopping:
                    break
                log.info(f"disconnected from {self.active_host}")
                self._set_connected(False)
                # Reconnect immediately once: covers Kodi returning to the
                # foreground after the TV switched inputs/apps.
                continue
            if self.pairing_blocked or self._stopping:
                continue
            delay = self._backoff.next()
            log.debug(f"retrying in {delay:.0f}s")
            await self._wait_wake(delay)

    async def _wait_wake(self, timeout: float | None) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._wake.wait()
        self._wake.clear()

    def _candidate_hosts(self) -> list[str]:
        configured = self.settings.host
        if not (self.settings.is_loopback and self.settings.loopback_fallback):
            return [configured]
        runtime = storage.read_json(RUNTIME_FILE, {}) or {}
        remembered = runtime.get("fallback_host") if runtime.get("for_host") == configured else None
        if remembered:
            return [remembered, configured]
        return [configured, "__lan__"]

    async def _connect_cycle(self) -> bool:
        """Try each candidate host once. Return True when connected."""
        self._wake.clear()
        for index, host in enumerate(self._candidate_hosts()):
            if host == "__lan__":
                host = self._lan_ip()
                if not host:
                    log.warning("loopback refused and no LAN IP found for fallback")
                    break
                log.info(f"loopback refused; falling back to LAN IP {host}")
            try:
                await self._connect(host)
            except WebOsTvPairError as err:
                self._handle_pair_error(err)
                return False
            except asyncio.CancelledError:
                raise
            except CONNECTION_ERRORS as err:
                if self._pairing_timed_out():
                    self._block_pairing("Pairing timed out on the TV")
                    return False
                log.debug(f"connect to {host} failed: {err!r}")
                if index == 0 and not self._failure_notified:
                    log.warning(f"cannot reach TV at {host}: {err!r}")
                continue
            self._remember_host(host)
            return True
        if not self._failure_notified:
            self._failure_notified = True
            if self.settings.toast_on_connect:
                self._notify("error", f"Cannot reach TV at {self.settings.host}")
        self._set_connected(False)
        return False

    async def _connect(self, host: str) -> None:
        await self._disconnect_client()
        key = self.keystore.get(self.settings.host)
        self._had_key = key is not None
        self.active_host = host
        client = self._client_factory(
            host, client_key=key, connect_timeout=self.settings.connect_timeout
        )
        self.client = client
        await client.register_state_update_callback(self._on_client_state)
        log.debug(f"connecting to {host} (paired={key is not None})")
        if not await client.connect():
            # disconnect() raced the attempt (re-pair, settings change, stop)
            raise ConnectionError("connect cancelled")
        if client.client_key and client.client_key != key:
            self.keystore.set(self.settings.host, client.client_key)
            log.info("pairing accepted; client key stored")
        self._failure_notified = False
        log.info(f"connected to {host}")
        self._set_connected(True)
        if self.settings.toast_on_connect:
            self._notify("info", f"Connected to TV at {host}")

    def _pairing_timed_out(self) -> bool:
        client = self.client
        return (
            client is not None
            and not self._had_key
            and bool(client.tv_info.hello)
            and not client.client_key
        )

    def _handle_pair_error(self, err: WebOsTvPairError) -> None:
        if self._had_key:
            self.keystore.delete(self.settings.host)
            self._block_pairing("Stored pairing key was rejected; use Re-pair")
        else:
            self._block_pairing(f"Pairing declined ({err})")

    def _block_pairing(self, message: str) -> None:
        log.warning(message)
        self.pairing_blocked = True
        self._notify("error", message)
        self._set_connected(False)

    def _remember_host(self, host: str) -> None:
        if not self.settings.is_loopback:
            return
        if host == self.settings.host:
            storage.write_json(RUNTIME_FILE, {})
        else:
            log.info(f"using {host} instead of {self.settings.host}")
            storage.write_json(RUNTIME_FILE, {"for_host": self.settings.host, "fallback_host": host})

    async def _wait_disconnected(self) -> None:
        client = self.client
        task = client.connect_task if client else None
        if task is None:
            return
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            log.debug(f"connection ended: {task.exception()!r}")

    async def _disconnect_client(self) -> None:
        client = self.client
        if client is None:
            return
        self._connected = False
        with contextlib.suppress(Exception):
            await client.disconnect()

    # state ---------------------------------------------------------------

    def _set_connected(self, connected: bool) -> None:
        self._connected = connected
        self._publish()

    async def _on_client_state(self, _tv_state: Any) -> None:
        self._publish()

    def _publish(self) -> None:
        client = self.client
        connected = self.connected
        if client is not None:
            snapshot = TvState.from_client(
                client.tv_state,
                connected=connected,
                paired=self.keystore.get(self.settings.host) is not None,
                host=self.active_host,
                picture_preset=self.picture_preset if connected else "",
            )
        else:
            snapshot = TvState(
                host=self.active_host,
                paired=self.keystore.get(self.settings.host) is not None,
            )
        self.state = snapshot
        try:
            self._on_state(snapshot)
        except Exception as err:  # noqa: BLE001
            log.error(f"state publish failed: {err!r}")

    # commands ------------------------------------------------------------

    def _require(self) -> WebOsClient:
        if not self.connected or self.client is None:
            raise Disconnected
        return self.client

    async def _call(self, method: str, *args: Any) -> Any:
        client = self._require()
        try:
            return await getattr(client, method)(*args)
        except WebOsTvCommandError as err:
            log.warning(f"{method} failed: {err}")
            raise CommandFailed(str(err)) from err
        except (OSError, aiohttp.ClientError) as err:
            raise Disconnected(str(err)) from err

    async def power_off(self) -> None:
        await self._call("power_off")

    async def power_on(self) -> None:
        if self.connected and not self.state.screen_on and self.state.is_on:
            await self._call("set_screen_state", True)
            return
        if not self.settings.mac:
            raise BadRequest("power_on needs the TV MAC address (off-TV mode)")
        try:
            wol.send_magic_packet(self.settings.mac)
        except ValueError as err:
            raise BadRequest(str(err)) from err
        log.info("sent Wake-on-LAN packet")
        self._backoff.reset()
        self._wake.set()

    async def screen(self, on: bool) -> None:
        await self._call("set_screen_state", on)

    async def volume(self, value: int | str) -> None:
        if value == "up":
            await self._call("volume_up")
        elif value == "down":
            await self._call("volume_down")
        else:
            await self._call("set_volume", int(value))

    async def mute(self, mode: str) -> None:
        if mode == "toggle":
            client = self._require()
            muted = not bool(client.tv_state.muted)
        else:
            muted = mode == "on"
        await self._call("set_mute", muted)

    def resolve_input(self, query: str) -> str:
        self._require()
        q = query.casefold()
        for input_id, label, app_id in self.state.inputs:
            if q in (input_id.casefold(), label.casefold(), app_id.casefold()):
                return input_id
        raise BadRequest(f"unknown input: {query}")

    async def set_input(self, query: str) -> str:
        input_id = self.resolve_input(query)
        await self._call("set_input", input_id)
        return input_id

    def resolve_app(self, query: str) -> str:
        self._require()
        q = query.casefold()
        apps = self.state.apps
        for app_id, title in apps:
            if q == app_id.casefold() or q == title.casefold():
                return app_id
        partial = [app_id for app_id, title in apps if q in app_id.casefold() or q in title.casefold()]
        if len(partial) == 1:
            return partial[0]
        if partial:
            raise BadRequest(f"ambiguous app {query!r}: {', '.join(sorted(partial))}")
        raise BadRequest(f"unknown app: {query}")

    async def launch_app(self, query: str, params: dict[str, Any] | None = None) -> str:
        app_id = self.resolve_app(query)
        if params:
            await self._call("launch_app_with_params", app_id, params)
        else:
            await self._call("launch_app", app_id)
        return app_id

    async def sound_output(self, output: str) -> None:
        await self._call("change_sound_output", output)

    async def button(self, name: str) -> None:
        name = name.upper()
        if name not in BUTTONS:
            raise BadRequest(f"unknown button: {name}")
        await self._call("button", name)

    async def toast(self, message: str) -> None:
        await self._call("send_message", message)

    async def media(self, command: str) -> None:
        method = MEDIA_COMMANDS.get(command)
        if method is None:
            raise BadRequest(f"unknown media command: {command}")
        await self._call(method)

    async def channel(self, value: str) -> None:
        if value == "up":
            await self._call("channel_up")
        elif value == "down":
            await self._call("channel_down")
        else:
            await self._call("set_channel", value)

    async def command(self, uri: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        uri = uri.removeprefix("ssap://")
        return await self._call("request", uri, payload)

    async def firmware(self) -> str:
        """Firmware version string, cached per connection.

        getCurrentSWInformation returns 401 on newer firmware (webOS 10+),
        so fall back to the tv.nyx.* config values, which stay readable.
        """
        client = self._require()
        if self._firmware is not None and self._firmware[0] is client:
            return self._firmware[1]
        text = ""
        software = client.tv_info.software
        if not software:
            with contextlib.suppress(CommandFailed):
                software = await self._call("get_software_info")
        if software:
            text = " ".join(
                str(software.get(k, "")) for k in ("product_name", "major_ver", "minor_ver")
            ).strip()
        if not text:
            with contextlib.suppress(CommandFailed):
                res = await self._call(
                    "request",
                    "config/getConfigs",
                    {"configNames": ["tv.nyx.firmwareVersion", "tv.nyx.platformVersion"]},
                )
                configs = res.get("configs") or {}
                version = configs.get("tv.nyx.firmwareVersion", "")
                platform = configs.get("tv.nyx.platformVersion", "")
                text = f"{version} (webOS {platform})" if version and platform else version
        self._firmware = (client, text)
        return text

    def status(self) -> dict[str, Any]:
        client = self.client
        system = client.tv_info.system if client else {}
        firmware = ""
        if self._firmware is not None and self._firmware[0] is client:
            firmware = self._firmware[1]
        return {
            "host": self.active_host,
            "configured_host": self.settings.host,
            "connected": self.connected,
            "paired": self.keystore.get(self.settings.host) is not None,
            "pairing_blocked": self.pairing_blocked,
            "model": system.get("modelName", "") if system else "",
            "firmware": firmware,
            "power": self.state.power,
            "volume": self.state.volume,
            "muted": self.state.muted,
            "app": self.state.app_id,
            "input": self.state.input_id,
            "sound_output": self.state.sound_output,
        }

    def catalog(self) -> dict[str, Any]:
        self._require()
        return {
            "inputs": [{"id": i, "label": label, "app_id": a} for i, label, a in self.state.inputs],
            "apps": sorted(
                ({"id": a, "title": t} for a, t in self.state.apps),
                key=lambda app: app["title"].casefold(),
            ),
            "current_app": self.state.app_id,
            "sound_output": self.state.sound_output,
        }
