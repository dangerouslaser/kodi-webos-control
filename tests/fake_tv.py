"""A fake LG webOS SSAP server for off-device tests.

Implements enough of the protocol for aiowebostv's connect() handshake,
request/subscribe for the endpoints we use, a pointer input socket, PROMPT
pairing, and scripted connection drops. Serves plain ws and (optionally)
wss with a throwaway self-signed certificate.
"""

from __future__ import annotations

import contextlib
import copy
import json
import ssl
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Response

NOT_FOUND = "404 no such service or method"

DEFAULT_INPUTS = [
    {"id": "HDMI_1", "label": "HDMI 1", "appId": "com.webos.app.hdmi1"},
    {"id": "HDMI_2", "label": "Apple TV", "appId": "com.webos.app.hdmi2"},
    # Unrenamed inputs aren't launch points on real TVs.
    {"id": "HDMI_3", "label": "HDMI 3", "appId": "com.webos.app.hdmi3"},
]
DEFAULT_APPS = [
    {"id": "netflix", "title": "Netflix"},
    {"id": "youtube.leanback.v4", "title": "YouTube"},
    {"id": "org.xbmc.kodi", "title": "Kodi"},
    {"id": "com.webos.app.hdmi1", "title": "HDMI 1"},
    {"id": "com.webos.app.hdmi2", "title": "Apple TV"},
]


@dataclass
class TvModel:
    """Mutable TV state the fake server reports and pushes."""

    power: str = "Active"
    volume: int = 10
    muted: bool = False
    app_id: str = "org.xbmc.kodi"
    sound_output: str = "tv_speaker"
    picture_mode: str = "filmMaker"
    inputs: list[dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(DEFAULT_INPUTS))
    apps: list[dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(DEFAULT_APPS))


def make_self_signed_cert(directory: Path) -> tuple[Path, Path]:
    """Create a throwaway self-signed cert with the openssl CLI."""
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=fake-webos-tv",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class FakeTv:
    """Fake SSAP server. Use ``await tv.start()`` / ``await tv.stop()``."""

    def __init__(
        self,
        *,
        ws: bool = True,
        wss_cert: tuple[Path, Path] | None = None,
        reject_ws_handshake: bool = False,
        pairing: str = "accept",
        valid_keys: set[str] | None = None,
    ) -> None:
        self.model = TvModel()
        self.enable_ws = ws
        self.wss_cert = wss_cert
        self.reject_ws_handshake = reject_ws_handshake
        # "accept", "reject" or "ignore" (never answer the prompt)
        self.pairing = pairing
        self.valid_keys: set[str] = set(valid_keys or ())
        self.ws_port: int | None = None
        self.wss_port: int | None = None
        self.prompts = 0
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.buttons: list[str] = []
        self.connections: set[ServerConnection] = set()
        self._servers: list[Server] = []
        self._subs: dict[str, list[tuple[ServerConnection, Any]]] = {}
        self._input_path = f"/input/{uuid.uuid4().hex}"
        self.fail_uris: dict[str, str] = {}
        self.uuid = str(uuid.uuid4())
        # Set True to model firmware that ignores the alert->luna workaround.
        self.luna_blocked = False
        # Picture modes this fake TV accepts.
        self.picture_modes = {"filmMaker", "cinema", "expert1", "vivid", "normal", "game"}
        self._alerts: dict[str, dict[str, Any]] = {}
        self._pending_pushes: list[str] = []
        self.luna_calls: list[tuple[str, dict[str, Any]]] = []

    # lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self.enable_ws:
            server = await serve(
                self._handler,
                "127.0.0.1",
                0,
                process_request=self._process_ws_request,
                compression=None,
            )
            self._servers.append(server)
            self.ws_port = server.sockets[0].getsockname()[1]
        if self.wss_cert is not None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*self.wss_cert)
            server = await serve(self._handler, "127.0.0.1", 0, ssl=ctx, compression=None)
            self._servers.append(server)
            self.wss_port = server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self._servers = []

    async def drop_all(self) -> None:
        """Abruptly close every client connection (scripted drop)."""
        for conn in list(self.connections):
            conn.transport.abort()
        self.connections.clear()
        self._subs.clear()

    # state pushes --------------------------------------------------------

    async def set_volume(self, volume: int) -> None:
        self.model.volume = volume
        await self._push("audio/getVolume")

    async def set_muted(self, muted: bool) -> None:
        self.model.muted = muted
        await self._push("audio/getStatus")

    async def set_app(self, app_id: str) -> None:
        self.model.app_id = app_id
        await self._push("com.webos.applicationManager/getForegroundAppInfo")

    async def set_power(self, state: str) -> None:
        self.model.power = state
        await self._push("com.webos.service.tvpower/power/getPowerState")

    async def _push(self, uri: str) -> None:
        for conn, uid in list(self._subs.get(uri, [])):
            payload = self._payload(uri, {})
            with contextlib.suppress(Exception):
                await conn.send(json.dumps({"type": "response", "id": uid, "payload": payload}))

    # protocol ------------------------------------------------------------

    def _process_ws_request(self, _conn: ServerConnection, _request: Any) -> Response | None:
        if self.reject_ws_handshake:
            return Response(400, "Bad Request", Headers(), b"use wss")
        return None

    async def _handler(self, conn: ServerConnection) -> None:
        self.connections.add(conn)
        try:
            if conn.request is not None and conn.request.path == self._input_path:
                await self._input_handler(conn)
                return
            async for raw in conn:
                await self._on_message(conn, json.loads(raw))
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.connections.discard(conn)
            for subs in self._subs.values():
                subs[:] = [s for s in subs if s[0] is not conn]

    async def _input_handler(self, conn: ServerConnection) -> None:
        async for raw in conn:
            fields = dict(
                line.split(":", 1) for line in str(raw).split("\n") if ":" in line
            )
            if fields.get("type") == "button":
                self.buttons.append(fields.get("name", ""))

    async def _on_message(self, conn: ServerConnection, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        uid = msg.get("id")
        if mtype == "hello":
            await conn.send(json.dumps({
                "type": "hello",
                "id": uid,
                "payload": {
                    "protocolVersion": 1, "deviceType": "tv", "deviceOS": "webOS",
                    "deviceUUID": self.uuid,
                },
            }))
            return
        if mtype == "register":
            await self._register(conn, msg)
            return
        if mtype == "unsubscribe":
            return
        if mtype not in ("request", "subscribe"):
            return

        uri = msg.get("uri", "").removeprefix("ssap://")
        payload = msg.get("payload") or {}
        self.requests.append((mtype, uri, payload))

        if uri in self.fail_uris:
            await conn.send(json.dumps({
                "type": "error", "id": uid, "error": self.fail_uris[uri], "payload": {},
            }))
            return

        if uri == "com.webos.service.networkinput/getPointerInputSocket":
            port = conn.local_address[1]
            scheme = "wss" if port == self.wss_port else "ws"
            await conn.send(json.dumps({
                "type": "response", "id": uid,
                "payload": {"returnValue": True, "socketPath": f"{scheme}://127.0.0.1:{port}{self._input_path}"},
            }))
            return

        reply = self._apply(uri, payload)
        if reply is None:
            await conn.send(json.dumps({
                "type": "error", "id": uid, "error": NOT_FOUND, "payload": {},
            }))
            return

        if mtype == "subscribe":
            self._subs.setdefault(uri, []).append((conn, uid))
            reply = {**reply, "subscribed": True}
        await conn.send(json.dumps({"type": "response", "id": uid, "payload": reply}))
        pushes = self._side_effects(uri) + self._pending_pushes
        self._pending_pushes = []
        for pushed in pushes:
            await self._push(pushed)

    async def _register(self, conn: ServerConnection, msg: dict[str, Any]) -> None:
        uid = msg.get("id")
        key = (msg.get("payload") or {}).get("client-key")
        if key and key in self.valid_keys:
            await conn.send(json.dumps({"type": "registered", "id": uid, "payload": {"client-key": key}}))
            return
        # Unknown or missing key: real TVs show the PROMPT dialog again.
        self.prompts += 1
        await conn.send(json.dumps({"type": "response", "id": uid, "payload": {"pairingType": "PROMPT", "returnValue": True}}))
        if self.pairing == "accept":
            new_key = uuid.uuid4().hex
            self.valid_keys.add(new_key)
            await conn.send(json.dumps({"type": "registered", "id": uid, "payload": {"client-key": new_key}}))
        elif self.pairing == "reject":
            await conn.send(json.dumps({"type": "error", "id": uid, "error": "403 User denied access", "payload": {}}))
        # "ignore": say nothing; client times out

    def _payload(self, uri: str, _payload: dict[str, Any]) -> dict[str, Any]:
        m = self.model
        table: dict[str, dict[str, Any]] = {
            "system/getSystemInfo": {"modelName": "OLED65FAKE", "returnValue": True},
            "com.webos.service.update/getCurrentSWInformation": {
                "product_name": "webOSTV 23", "major_ver": "03", "minor_ver": "30.50", "returnValue": True,
            },
            "com.webos.service.connectionmanager/getinfo": {
                "wifiInfo": {"macAddress": "E0:85:4D:00:00:02"},
                "wiredInfo": {"macAddress": "58:96:0a:00:00:01"},
                "returnValue": True,
            },
            "com.webos.service.tvpower/power/getPowerState": {"state": m.power, "returnValue": True},
            "com.webos.applicationManager/getForegroundAppInfo": {"appId": m.app_id, "returnValue": True},
            "audio/getStatus": {"mute": m.muted, "volume": m.volume, "returnValue": True},
            "audio/getVolume": {"volumeStatus": {"volume": m.volume, "muteStatus": m.muted}, "returnValue": True},
            "com.webos.applicationManager/listLaunchPoints": {"launchPoints": m.apps, "returnValue": True},
            "tv/getExternalInputList": {"devices": m.inputs, "returnValue": True},
            "com.webos.service.apiadapter/audio/getSoundOutput": {"soundOutput": m.sound_output, "returnValue": True},
            "com.webos.media/getForegroundAppInfo": {"foregroundAppInfo": [], "returnValue": True},
            "settings/getSystemSettings": {
                "category": "picture", "settings": {"pictureMode": m.picture_mode}, "returnValue": True,
            },
        }
        return table.get(uri, {})

    def _apply(self, uri: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Apply a command to the model; return the response payload."""
        m = self.model
        ok = {"returnValue": True}
        if uri in {
            "system/getSystemInfo",
            "com.webos.service.update/getCurrentSWInformation",
            "com.webos.service.connectionmanager/getinfo",
            "com.webos.service.tvpower/power/getPowerState",
            "com.webos.applicationManager/getForegroundAppInfo",
            "audio/getStatus",
            "audio/getVolume",
            "com.webos.applicationManager/listLaunchPoints",
            "tv/getExternalInputList",
            "com.webos.service.apiadapter/audio/getSoundOutput",
            "com.webos.media/getForegroundAppInfo",
            "settings/getSystemSettings",
        }:
            return self._payload(uri, payload)
        if uri == "system.notifications/createAlert":
            alert_id = f"alert-{uuid.uuid4().hex[:8]}"
            self._alerts[alert_id] = payload
            return {"alertId": alert_id, **ok}
        if uri == "system.notifications/closeAlert":
            alert = self._alerts.pop(payload.get("alertId"), None)
            if alert is None:
                return None
            onclose = alert.get("onclose") or {}
            self._luna(str(onclose.get("uri", "")), onclose.get("params") or {})
            return ok
        if uri == "config/getConfigs":
            known = {"tv.nyx.firmwareVersion": "33.30.97", "tv.nyx.platformVersion": "10.3.0"}
            names = payload.get("configNames", [])
            return {"configs": {k: v for k, v in known.items() if k in names}, **ok}
        if uri == "audio/setVolume":
            m.volume = int(payload["volume"])
            return ok
        if uri == "audio/volumeUp":
            m.volume = min(100, m.volume + 1)
            return ok
        if uri == "audio/volumeDown":
            m.volume = max(0, m.volume - 1)
            return ok
        if uri == "audio/setMute":
            m.muted = bool(payload["mute"])
            return ok
        if uri == "tv/switchInput":
            match = [i for i in m.inputs if i["id"] == payload.get("inputId")]
            if not match:
                return None
            m.app_id = match[0]["appId"]
            return ok
        if uri == "system.launcher/launch":
            m.app_id = payload["id"]
            return ok
        if uri == "com.webos.service.apiadapter/audio/changeSoundOutput":
            m.sound_output = payload["output"]
            return ok
        if uri == "system.notifications/createToast":
            return {"toastId": "1", **ok}
        if uri == "system/turnOff":
            m.power = "Suspend"
            return ok
        if uri in ("com.webos.service.tvpower/power/turnOffScreen",):
            m.power = "Screen Off"
            return ok
        if uri in ("com.webos.service.tvpower/power/turnOnScreen",):
            m.power = "Active"
            return ok
        if uri.startswith("media.controls/") or uri in ("tv/channelUp", "tv/channelDown", "tv/openChannel"):
            return ok
        return None

    def _luna(self, uri: str, params: dict[str, Any]) -> None:
        self.luna_calls.append((uri, params))
        if self.luna_blocked:
            return
        if uri == "luna://com.webos.settingsservice/setSystemSettings" and params.get("category") == "picture":
            mode = (params.get("settings") or {}).get("pictureMode")
            if mode in self.picture_modes:
                self.model.picture_mode = mode
                self._pending_pushes.append("settings/getSystemSettings")

    async def set_picture_mode(self, mode: str) -> None:
        """Simulate the user (or an HDR signal) changing the picture mode."""
        self.model.picture_mode = mode
        await self._push("settings/getSystemSettings")

    def _side_effects(self, uri: str) -> list[str]:
        return {
            "audio/setVolume": ["audio/getVolume", "audio/getStatus"],
            "audio/volumeUp": ["audio/getVolume", "audio/getStatus"],
            "audio/volumeDown": ["audio/getVolume", "audio/getStatus"],
            "audio/setMute": ["audio/getStatus", "audio/getVolume"],
            "tv/switchInput": ["com.webos.applicationManager/getForegroundAppInfo"],
            "system.launcher/launch": ["com.webos.applicationManager/getForegroundAppInfo"],
            "com.webos.service.apiadapter/audio/changeSoundOutput": ["com.webos.service.apiadapter/audio/getSoundOutput"],
            "system/turnOff": ["com.webos.service.tvpower/power/getPowerState"],
            "com.webos.service.tvpower/power/turnOffScreen": ["com.webos.service.tvpower/power/getPowerState"],
            "com.webos.service.tvpower/power/turnOnScreen": ["com.webos.service.tvpower/power/getPowerState"],
        }.get(uri, [])


class FakeSsdp:
    """Answers SSDP M-SEARCH on a unicast port, plus a description server.

    ``devices`` maps a reported host to (uuid, friendlyName, modelNumber).
    Responses come from 127.0.0.1, so the reported host is put in a custom
    header the tests read back via ``source_override``.
    """

    def __init__(self, uuid_: str, name: str = "[LG] webOS TV OLED77FAKE",
                 model: str = "OLED77FAKE.AUS", *, extra_noise: bool = True) -> None:
        self.uuid = uuid_
        self.name = name
        self.model = model
        self.extra_noise = extra_noise
        self.port: int | None = None
        self.searches = 0
        self._transport: Any = None
        self._http: Any = None
        self._http_port: int | None = None

    async def start(self) -> None:
        import asyncio

        self._http = await asyncio.start_server(self._serve_description, "127.0.0.1", 0)
        self._http_port = self._http.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        outer = self

        class Proto(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                if b"M-SEARCH" not in data:
                    return
                outer.searches += 1
                if outer.extra_noise:
                    # A Hue-bridge style responder that answers everything.
                    self.transport.sendto(
                        b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\n"
                        b"USN: uuid:2f402f80-0000::upnp:rootdevice\r\n\r\n", addr)
                body = (
                    "HTTP/1.1 200 OK\r\n"
                    f"Location: http://127.0.0.1:{outer._http_port}/\r\n"
                    f"USN: uuid:{outer.uuid}::urn:lge-com:service:webos-second-screen:1\r\n"
                    "ST: urn:lge-com:service:webos-second-screen:1\r\n\r\n"
                )
                self.transport.sendto(body.encode(), addr)

        self._transport, _ = await loop.create_datagram_endpoint(Proto, local_addr=("127.0.0.1", 0))
        self.port = self._transport.get_extra_info("sockname")[1]

    async def _serve_description(self, reader, writer) -> None:
        await reader.readuntil(b"\r\n\r\n")
        xml = (
            "<?xml version=\"1.0\"?><root><device>"
            f"<friendlyName>{self.name}</friendlyName>"
            f"<modelNumber>{self.model}</modelNumber>"
            f"<UDN>uuid:{self.uuid}</UDN></device></root>"
        ).encode()
        writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: text/xml\r\n"
                     + f"Content-Length: {len(xml)}\r\n\r\n".encode() + xml)
        await writer.drain()
        writer.close()

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
        if self._http is not None:
            self._http.close()
            await self._http.wait_closed()
