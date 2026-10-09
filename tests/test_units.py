"""Unit tests: action parsing, backoff, key storage, state diffing, misc."""

from __future__ import annotations

import json

import pytest
import xbmc
import xbmcgui

from resources.lib import actions, ipc, kodilog, wol
from resources.lib.errors import BadRequest
from resources.lib.net import Backoff
from resources.lib.pairing import KeyStore
from resources.lib.state import PropertyPublisher, TvState


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["power_off"], {}),
        (["screen", "off"], {"value": "off"}),
        (["volume", "25"], {"value": 25}),
        (["volume", "UP"], {"value": "up"}),
        (["mute"], {"value": "toggle"}),
        (["mute", "on"], {"value": "on"}),
        (["input", "HDMI_2"], {"value": "HDMI_2"}),
        (["app", "netflix"], {"value": "netflix", "params": None}),
        (["app", "youtube", '{"contentTarget":"v=abc"', '"x":1}'],
         {"value": "youtube", "params": {"contentTarget": "v=abc", "x": 1}}),
        (["toast", "Hello", " world"], {"value": "Hello, world"}),
        (["button", "HOME"], {"value": "HOME"}),
        (["command", "ssap://system/getSystemInfo"],
         {"uri": "ssap://system/getSystemInfo", "payload": None}),
        # RunScript splits on commas, so JSON payloads arrive in pieces.
        (["command", "ssap://audio/setVolume", '{"volume": 5', ' "x": [1', "2]}"],
         {"uri": "ssap://audio/setVolume", "payload": {"volume": 5, "x": [1, 2]}}),
        (["command", "audio/getVolume", {"a": 1}], {"uri": "audio/getVolume", "payload": {"a": 1}}),
        (["channel", "Up"], {"value": "up"}),
        (["media", "fast_forward"], {"value": "fast_forward"}),
    ],
)
def test_parse_valid(argv, expected):
    action, kwargs = actions.parse(argv[0], argv[1:])
    assert action.name == argv[0]
    assert kwargs == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["nope"],
        ["power_off", "now"],
        ["screen"],
        ["screen", "dim"],
        ["volume", "101"],
        ["volume", "-1"],
        ["volume", "loud"],
        ["volume"],
        ["mute", "maybe"],
        ["input"],
        ["toast"],
        ["command"],
        ["command", "http://evil"],
        ["command", "ssap://x", "{not json"],
        ["command", "ssap://x", "[1,2]"],
        ["app", ""],
    ],
)
def test_parse_invalid(argv):
    with pytest.raises(BadRequest):
        actions.parse(argv[0], argv[1:])


def test_registry_has_spec_actions():
    for name in ("power_off", "power_on", "screen", "volume", "mute", "input", "app",
                 "sound_output", "button", "toast", "command"):
        assert name in actions.ACTIONS
    assert "ui" in actions.LOCAL_ACTIONS


def test_backoff_doubles_to_cap_and_resets():
    b = Backoff(base=2, cap=60)
    assert [b.next() for _ in range(7)] == [2, 4, 8, 16, 32, 60, 60]
    b.reset()
    assert b.next() == 2


def test_keystore_round_trip_per_host():
    ks = KeyStore()
    assert ks.get("10.0.0.5") is None
    ks.set("10.0.0.5", "abc")
    ks.set("127.0.0.1", "def")
    assert KeyStore().get("10.0.0.5") == "abc"
    assert ks.delete("10.0.0.5")
    assert ks.get("10.0.0.5") is None
    assert ks.get("127.0.0.1") == "def"
    assert not ks.delete("10.0.0.5")


def test_publisher_writes_only_changes():
    pub = PropertyPublisher()
    pub.publish(TvState(connected=True, host="h", volume=10, muted=False))
    xbmcgui.writes.clear()
    changed = pub.publish(TvState(connected=True, host="h", volume=11, muted=False))
    assert changed == ["Volume"]
    assert xbmcgui.writes == [("WebOS.Volume", "11")]
    assert xbmcgui.Window(10000).getProperty("WebOS.Volume") == "11"
    xbmcgui.writes.clear()
    assert pub.publish(TvState(connected=True, host="h", volume=11, muted=False)) == []
    assert xbmcgui.writes == []


def test_log_redacts_client_key():
    kodilog.info('register {"client-key": "SECRET123", "x": 1}')
    assert "SECRET123" not in xbmc.logs[-1][1]
    assert '"client-key": "***"' in xbmc.logs[-1][1]
    assert xbmc.logs[-1][1].startswith("[service.webostv] ")


def test_debug_gated():
    kodilog.set_debug(False)
    kodilog.debug("hidden")
    assert not any("hidden" in m for _, m in xbmc.logs)
    kodilog.set_debug(True)
    kodilog.debug("shown")
    kodilog.set_debug(False)
    assert any("shown" in m for _, m in xbmc.logs)


def test_magic_packet():
    pkt = wol.magic_packet("AA:BB:CC:DD:EE:FF")
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("AABBCCDDEEFF")
    assert len(pkt) == 102
    assert wol.magic_packet("aabbccddeeff") == pkt
    with pytest.raises(ValueError):
        wol.magic_packet("nope")


def test_parse_notification_filters_sender_and_decodes():
    data = json.dumps({"id": "1", "action": "x"})
    assert ipc.parse_notification("other.addon", "Other.request", data) is None
    assert ipc.parse_notification("service.webostv", "Other.request", data) == (
        "request", {"id": "1", "action": "x"})
    double = json.dumps(data)
    assert ipc.parse_notification("service.webostv", "Other.reply", double)[0] == "reply"
    assert ipc.parse_notification("service.webostv", "Other.request", "garbage") is None
    assert ipc.parse_notification("service.webostv", "Other.other", data) is None
