"""End-to-end: RunScript argv -> NotifyAll -> service -> fake TV -> reply."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
import xbmc
import xbmcaddon
import xbmcgui
from fake_tv import FakeTv

from resources.lib import bootstrap, ipc, script
from resources.lib.service import WebOsService


class TvThread:
    """Run a FakeTv on its own loop thread (the service has its own loop)."""

    def __init__(self, **kwargs):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.tv = FakeTv(**kwargs)
        self.run(self.tv.start())

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(5)

    def close(self):
        self.run(self.tv.stop())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met")


@pytest.fixture
def running(patch_ports):
    xbmcaddon.settings["loopback_fallback"] = "false"
    tvt = TvThread()
    patch_ports(tvt.tv)
    service = WebOsService()
    assert service.start()
    wait_until(lambda: service.controller.connected)
    yield service, tvt.tv
    service.shutdown()
    tvt.close()


def test_runscript_fire_and_forget(running):
    _service, tv = running
    script.main(["default.py", "volume", "25"])
    wait_until(lambda: tv.model.volume == 25)
    wait_until(lambda: xbmcgui.Window(10000).getProperty("WebOS.Volume") == "25")

    script.main(["default.py", "button", "HOME"])
    wait_until(lambda: tv.buttons == ["HOME"])

    script.main(["default.py", "input", "Apple TV"])
    wait_until(lambda: tv.model.app_id == "com.webos.app.hdmi2")
    wait_until(lambda: xbmcgui.Window(10000).getProperty("WebOS.Input") == "HDMI_2")


def test_command_waits_for_reply(running):
    reply = script.run_action("command", ["ssap://system/getSystemInfo"])
    assert reply["ok"]
    assert reply["result"]["modelName"] == "OLED65FAKE"
    assert xbmcgui.textviewers and "OLED65FAKE" in xbmcgui.textviewers[-1][1]


def test_status_reply(running):
    reply = ipc.call("status")
    assert reply["ok"]
    status = reply["result"]
    assert status["connected"] is True
    assert status["paired"] is True
    assert status["model"] == "OLED65FAKE"
    assert "webOSTV 23" in status["firmware"]


def test_bad_request_never_reaches_tv(running):
    _service, tv = running
    before = len(tv.requests)
    reply = ipc.call("volume", ["loud"])
    assert reply == {"id": reply["id"], "ok": False, "error": "bad_request", "message": reply["message"]}
    assert len(tv.requests) == before
    # RunScript path rejects locally with a notification.
    script.main(["default.py", "volume", "loud"])
    assert xbmcgui.notifications[-1][2] == xbmcgui.NOTIFICATION_ERROR


def test_command_error_reply_carries_tv_text(running):
    _service, tv = running
    tv.fail_uris["system/getSystemInfo"] = "500 Application error"
    reply = ipc.call("command", ["ssap://system/getSystemInfo"])
    assert reply["error"] == "command_error"
    assert "500 Application error" in reply["message"]


def test_fire_and_forget_error_shows_notification(running):
    script.main(["default.py", "app", "no-such-app"])
    wait_until(lambda: any("no-such-app" in n[1] for n in xbmcgui.notifications))


def test_disconnected_fails_fast(running):
    service, tv = running
    asyncio.run_coroutine_threadsafe(service.controller.stop(), service.loop).result(2)
    reply = ipc.call("volume", ["5"])
    assert reply["error"] == "disconnected"


def test_service_ignores_other_senders(running):
    _service, tv = running
    before = len(tv.requests)
    xbmc.executeJSONRPC(
        '{"jsonrpc":"2.0","id":1,"method":"JSONRPC.NotifyAll","params":'
        '{"sender":"someone.else","message":"request","data":{"id":"1","action":"power_off","args":[]}}}'
    )
    time.sleep(0.1)
    assert len(tv.requests) == before


def test_call_times_out_without_service():
    reply = ipc.call("status", timeout=0.1)
    assert reply["error"] == "timeout"


def test_python_too_old_disables_cleanly(monkeypatch):
    monkeypatch.setattr(bootstrap, "python_supported", lambda: False)
    service = WebOsService()
    assert service.start() is False
    assert service.loop is None
    assert xbmcgui.notifications[-1][2] == xbmcgui.NOTIFICATION_ERROR
    assert any("too old" in m for _, m in xbmc.logs)


def test_shutdown_is_fast(patch_ports):
    xbmcaddon.settings["loopback_fallback"] = "false"
    tvt = TvThread()
    patch_ports(tvt.tv)
    service = WebOsService()
    service.start()
    wait_until(lambda: service.controller.connected)
    started = time.monotonic()
    service.shutdown()
    assert time.monotonic() - started < 2.0
    assert not service_thread_alive(service)
    tvt.close()


def service_thread_alive(service):
    return service.thread is not None and service.thread.is_alive()


def test_settings_change_reconnects(running):
    service, tv = running
    first = service.controller.client
    xbmcaddon.settings["toast_on_connect"] = "true"
    service.reload_settings()
    time.sleep(0.1)
    assert service.controller.client is first  # no reconnect for non-host settings
    assert service.controller.settings.toast_on_connect


def test_ui_input_and_status_flow(running):
    _service, tv = running
    # Main menu -> Inputs (0) -> "Apple TV" (1) -> main menu -> Status (6) -> cancel
    xbmcgui.select_answers.extend([0, 1, 6, -1])
    script.main(["default.py"])
    wait_until(lambda: tv.model.app_id == "com.webos.app.hdmi2")
    heading, labels = xbmcgui.selects[1]
    assert labels == ["HDMI 1", "Apple TV", "HDMI 3"]
    assert "Connected: yes" in xbmcgui.textviewers[-1][1]


def test_ui_remote_sends_buttons(running):
    _service, tv = running
    # Main menu -> Remote (4) -> HOME (0) -> BACK (1) -> back out -> cancel
    xbmcgui.select_answers.extend([4, 0, 1, -1, -1])
    script.main(["default.py", "ui"])
    wait_until(lambda: tv.buttons == ["HOME", "BACK"])


def test_service_saves_learned_mac_to_settings(running):
    assert xbmcaddon.settings["mac"] == "58:96:0A:00:00:01"


def test_picture_preset_action_and_player_hook(running):
    service, tv = running
    reply = ipc.call("picture_preset", ["cinema"])
    assert reply["ok"] and reply["result"] == "cinema"
    wait_until(lambda: xbmcgui.Window(10000).getProperty("WebOS.PicturePreset") == "cinema")

    # Enable rules via settings and drive the Kodi player hook.
    ipc.call("picture_preset", ["filmMaker"])
    xbmcaddon.settings.update({"rules_enabled": "true", "preset_hdr10": "expert1",
                               "rules_debounce": "0.5"})
    service.reload_settings()
    xbmc.info_labels.update({"_playing_video": "1", "VideoPlayer.HdrType": "hdr10"})
    service.player.onAVStarted()
    wait_until(lambda: tv.model.picture_mode == "expert1")
    service.player.onPlayBackStopped()
    wait_until(lambda: tv.model.picture_mode == "filmMaker")


def test_choose_rule_preset_from_tv_list(running):
    # "Choose HDR10 preset" button: list shows only HDR presets from the TV.
    xbmcgui.select_answers.append(1)  # 0 = No change, 1 = first HDR preset
    script.main(["default.py", "choose_preset", "hdr10"])
    heading, labels = xbmcgui.selects[-1]
    assert labels == ["No change  [current]", "Cinema (HDR)", "Filmmaker Mode (HDR)"]
    assert xbmcaddon.settings["preset_hdr10"] == "hdrCinema"
    assert xbmc.builtins[-1] == "Addon.OpenSettings(service.webostv)"

    # Picking "No change" clears it.
    xbmcgui.select_answers.append(0)
    script.main(["default.py", "choose_preset", "hdr10"])
    assert xbmcaddon.settings["preset_hdr10"] == ""


def test_menu_picture_preset_lists_current_signal_presets(running):
    _service, tv = running
    # Main menu -> Picture preset (3) -> pick "Cinema" -> cancel
    xbmcgui.select_answers.extend([3])
    labels_holder = {}

    import resources.lib.ui as ui

    real_select = xbmcgui.Dialog.select

    def select(self, heading, items, *a, **k):
        if heading == "Picture preset":
            labels_holder["labels"] = list(items)
            return items.index("Cinema")
        return real_select(self, heading, items, *a, **k)

    xbmcgui.Dialog.select = select
    try:
        xbmcgui.select_answers.append(-1)
        ui.run()
    finally:
        xbmcgui.Dialog.select = real_select
    assert "Filmmaker Mode  [current]" in labels_holder["labels"]
    assert not any("(HDR)" in label for label in labels_holder["labels"])
    wait_until(lambda: tv.model.picture_mode == "cinema")
