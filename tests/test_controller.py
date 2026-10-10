"""TvController against the fake TV: pairing, keys, reconnect, fallback."""

from __future__ import annotations

import asyncio

import pytest
import xbmcgui
from fake_tv import FakeTv

from resources.lib import storage
from resources.lib.errors import BadRequest, Disconnected
from resources.lib.pairing import KeyStore
from resources.lib.settings import Settings
from resources.lib.state import PropertyPublisher
from resources.lib.tv import TvController


async def wait_for(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def tv(patch_ports):
    tv = FakeTv()
    await tv.start()
    patch_ports(tv)
    yield tv
    await tv.stop()


def make_controller(settings=None, **kwargs):
    publisher = PropertyPublisher()
    notes = []
    kwargs.setdefault("backoff_base", 0.05)
    controller = TvController(
        settings or Settings(host="127.0.0.1", loopback_fallback=False),
        on_state=publisher.publish,
        notify=lambda kind, msg: notes.append((kind, msg)),
        **kwargs,
    )
    return controller, notes


async def test_first_connect_pairs_and_stores_key(tv):
    controller, _ = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        key = KeyStore().get("127.0.0.1")
        assert key in tv.valid_keys
        home = xbmcgui.Window(10000)
        await wait_for(lambda: home.getProperty("WebOS.Volume") == "10")
        assert home.getProperty("WebOS.Connected") == "true"
        assert home.getProperty("WebOS.App") == "org.xbmc.kodi"
        assert home.getProperty("WebOS.AppTitle") == "Kodi"
    finally:
        await controller.stop()
    assert xbmcgui.Window(10000).getProperty("WebOS.Connected") == "false"


async def test_restart_reuses_key_without_prompt(tv):
    controller, _ = make_controller()
    await controller.start()
    await wait_for(lambda: controller.connected)
    await controller.stop()

    controller2, _ = make_controller()
    await controller2.start()
    try:
        await wait_for(lambda: controller2.connected)
        assert tv.prompts == 1
    finally:
        await controller2.stop()


async def test_volume_push_updates_property_without_polling(tv):
    controller, _ = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        await tv.set_volume(42)
        home = xbmcgui.Window(10000)
        await wait_for(lambda: home.getProperty("WebOS.Volume") == "42", timeout=1.0)
    finally:
        await controller.stop()


async def test_reconnects_after_drop(tv):
    controller, _ = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        first = controller.client
        await tv.drop_all()
        await wait_for(lambda: controller.connected and controller.client is not first)
        assert tv.prompts == 1
    finally:
        await controller.stop()


async def test_backoff_when_unreachable_then_connects(patch_ports):
    tv = FakeTv()
    await tv.start()
    patch_ports(tv)
    await tv.stop()  # port now refuses connections
    controller, _ = make_controller()
    await controller.start()
    try:
        await asyncio.sleep(0.3)
        assert not controller.connected
        assert xbmcgui.Window(10000).getProperty("WebOS.Connected") == "false"
    finally:
        await controller.stop()


async def test_pairing_declined_blocks_until_repair(patch_ports):
    tv = FakeTv(pairing="reject")
    await tv.start()
    patch_ports(tv)
    controller, notes = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.pairing_blocked)
        await asyncio.sleep(0.3)
        assert tv.prompts == 1  # no retry loop
        assert any("declined" in m for _, m in notes)

        tv.pairing = "accept"
        await controller.repair()
        await wait_for(lambda: controller.connected)
        assert tv.prompts == 2
    finally:
        await controller.stop()
        await tv.stop()


async def test_rejected_stored_key_is_deleted_and_blocks(tv):
    KeyStore().set("127.0.0.1", "stale-key")
    tv.pairing = "reject"
    controller, notes = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.pairing_blocked)
        await asyncio.sleep(0.2)
        assert KeyStore().get("127.0.0.1") is None
        assert tv.prompts == 1
        assert len(notes) == 1
        assert "Re-pair" in notes[0][1]
    finally:
        await controller.stop()


async def test_stale_key_replaced_when_user_accepts_prompt(tv):
    KeyStore().set("127.0.0.1", "stale-key")
    controller, _ = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        assert KeyStore().get("127.0.0.1") in tv.valid_keys
    finally:
        await controller.stop()


async def test_repair_while_connected_prompts_once(tv):
    controller, _ = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        old_key = KeyStore().get("127.0.0.1")
        await controller.repair()
        await wait_for(lambda: controller.connected and KeyStore().get("127.0.0.1") != old_key)
        assert tv.prompts == 2
    finally:
        await controller.stop()


async def test_loopback_falls_back_to_lan_ip_and_persists(patch_ports, monkeypatch):
    tv = FakeTv()
    await tv.start()
    patch_ports(tv)
    attempts = []

    # Simulate a TV that refuses loopback: route "127.0.0.1" to a dead
    # address and the "LAN IP" to the fake server.
    from aiowebostv import WebOsClient

    def factory(host, **kw):
        attempts.append(host)
        real_host = "127.0.0.2" if host == "127.0.0.1" else "127.0.0.1"
        client = WebOsClient(real_host, **kw)
        client.host = real_host
        return client

    # 127.0.0.2 isn't routable on macOS; make it fail fast.
    import socket

    def _closed():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    from aiowebostv import webos_client

    real_ws_connect = webos_client.WebOsClient._ws_connect

    async def ws_connect(self, uri, max_msg_size):
        if "127.0.0.2" in uri:
            uri = uri.replace("127.0.0.2", "127.0.0.1").rsplit(":", 1)[0] + f":{_closed()}"
        return await real_ws_connect(self, uri, max_msg_size)

    monkeypatch.setattr(webos_client.WebOsClient, "_ws_connect", ws_connect)

    controller, _ = make_controller(
        Settings(host="127.0.0.1", loopback_fallback=True),
        client_factory=factory,
        lan_ip=lambda: "192.168.1.50",
    )
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        assert attempts[:2] == ["127.0.0.1", "192.168.1.50"]
        assert controller.active_host == "192.168.1.50"
        assert storage.read_json("runtime.json")["fallback_host"] == "192.168.1.50"
        # Key is stored under the configured host, not the fallback.
        assert KeyStore().get("127.0.0.1") is not None
    finally:
        await controller.stop()
        await tv.stop()

    # Next start goes straight to the remembered host.
    attempts.clear()
    await tv.start()
    patch_ports(tv)
    controller, _ = make_controller(
        Settings(host="127.0.0.1", loopback_fallback=True),
        client_factory=factory,
        lan_ip=lambda: "192.168.1.50",
    )
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        assert attempts[0] == "192.168.1.50"
    finally:
        await controller.stop()
        await tv.stop()


async def test_commands_and_resolution(tv):
    controller, _ = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        await controller.volume(30)
        await controller.volume("up")
        await wait_for(lambda: tv.model.volume == 31)
        await controller.mute("toggle")
        await wait_for(lambda: tv.model.muted)
        assert await controller.set_input("apple tv") == "HDMI_2"
        assert await controller.set_input("hdmi_1") == "HDMI_1"
        assert await controller.launch_app("netflix") == "netflix"
        assert await controller.launch_app("YOUTUBE") == "youtube.leanback.v4"
        with pytest.raises(BadRequest):
            await controller.launch_app("hdmi")  # ambiguous
        with pytest.raises(BadRequest):
            await controller.set_input("HDMI_9")
        await controller.sound_output("external_arc")
        assert tv.model.sound_output == "external_arc"
        await controller.button("home")
        await wait_for(lambda: tv.buttons == ["HOME"])
        with pytest.raises(BadRequest):
            await controller.button("SELF_DESTRUCT")
        await controller.toast("Hello")
        result = await controller.command("ssap://system/getSystemInfo")
        assert result["modelName"] == "OLED65FAKE"
        await controller.screen(False)
        await wait_for(lambda: tv.model.power == "Screen Off")
        await controller.power_off()
    finally:
        await controller.stop()


async def test_commands_fail_fast_when_disconnected():
    controller, _ = make_controller()
    with pytest.raises(Disconnected):
        await controller.volume(10)


async def test_pairing_prompt_timeout_blocks(patch_ports, monkeypatch):
    from aiowebostv import webos_client

    monkeypatch.setattr(webos_client, "RECEIVE_TIMEOUT", 0.2)
    tv = FakeTv(pairing="ignore")
    await tv.start()
    patch_ports(tv)
    controller, notes = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.pairing_blocked)
        await asyncio.sleep(0.3)
        assert tv.prompts == 1
        assert "timed out" in notes[0][1]
    finally:
        await controller.stop()
        await tv.stop()


async def test_firmware_from_software_info(tv):
    controller, _ = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        assert await controller.firmware() == "webOSTV 23 03 30.50"
    finally:
        await controller.stop()


async def test_firmware_falls_back_to_configs_when_sw_info_denied(tv):
    # webOS 10 answers getCurrentSWInformation with 401.
    tv.fail_uris["com.webos.service.update/getCurrentSWInformation"] = "401 insufficient permissions"
    controller, _ = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        assert await controller.firmware() == "33.30.97 (webOS 10.3.0)"
        assert controller.status()["firmware"] == "33.30.97 (webOS 10.3.0)"
    finally:
        await controller.stop()


async def test_app_title_falls_back_to_input_label(tv):
    controller, _ = make_controller()
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        await controller.set_input("HDMI_3")
        home = xbmcgui.Window(10000)
        await wait_for(lambda: home.getProperty("WebOS.Input") == "HDMI_3")
        assert home.getProperty("WebOS.AppTitle") == "HDMI 3"
    finally:
        await controller.stop()


async def test_learns_mac_when_setting_empty(tv):
    saved = []
    controller, _ = make_controller(on_mac=saved.append)
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        assert saved == ["58:96:0A:00:00:01"]  # wired preferred, upper-cased
        assert controller.settings.mac == "58:96:0A:00:00:01"
    finally:
        await controller.stop()


async def test_keeps_configured_mac(tv):
    saved = []
    controller, _ = make_controller(
        Settings(host="127.0.0.1", loopback_fallback=False, mac="AA:BB:CC:DD:EE:FF"),
        on_mac=saved.append,
    )
    await controller.start()
    try:
        await wait_for(lambda: controller.connected)
        assert saved == []
        assert controller.settings.mac == "AA:BB:CC:DD:EE:FF"
    finally:
        await controller.stop()


def test_reported_mac_picks_valid_wired_then_wifi():
    from resources.lib.tv import reported_mac

    assert reported_mac({"wiredInfo": {"macAddress": "bad"},
                         "wifiInfo": {"macAddress": "e0:85:4d:0f:fe:d6"}}) == "E0:85:4D:0F:FE:D6"
    assert reported_mac({}) == ""
    assert reported_mac(None) == ""
