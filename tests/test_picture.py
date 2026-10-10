"""Phase 2: picture presets via the alert->luna workaround, playback rules."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
import xbmcgui
from fake_tv import FakeTv

from resources.lib.automations import PlaybackRules, normalize_hdr_type
from resources.lib.errors import BadRequest, CommandFailed
from resources.lib.settings import Settings
from resources.lib.state import PropertyPublisher
from resources.lib.tv import TvController

BASE = Settings(host="127.0.0.1", loopback_fallback=False, auto_discover=False)


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


@pytest.fixture
async def controller(tv):
    publisher = PropertyPublisher()
    c = TvController(BASE, on_state=publisher.publish, backoff_base=0.05)
    await c.start()
    await wait_for(lambda: c.connected and c.picture_preset)
    yield c
    await c.stop()


async def test_picture_mode_followed_live(tv, controller):
    home = xbmcgui.Window(10000)
    assert controller.picture_preset == "filmMaker"
    await wait_for(lambda: home.getProperty("WebOS.PicturePreset") == "filmMaker")
    await tv.set_picture_mode("vivid")  # e.g. changed with the LG remote
    await wait_for(lambda: home.getProperty("WebOS.PicturePreset") == "vivid")


async def test_set_picture_mode_uses_alert_and_verifies(tv, controller):
    assert await controller.set_picture_mode("cinema") == "cinema"
    assert tv.model.picture_mode == "cinema"
    uri, params = tv.luna_calls[-1]
    assert uri == "luna://com.webos.settingsservice/setSystemSettings"
    assert params == {"category": "picture", "settings": {"pictureMode": "cinema"}}
    assert controller.status()["picture_preset"] == "cinema"


async def test_unknown_preset_is_reported(tv, controller, monkeypatch):
    from resources.lib import tv as tv_module

    monkeypatch.setattr(tv_module, "PRESET_VERIFY_TIMEOUT", 0.3)
    with pytest.raises(CommandFailed, match="did not apply"):
        await controller.set_picture_mode("notAMode")


async def test_blocked_firmware_is_reported(tv, controller, monkeypatch):
    from resources.lib import tv as tv_module

    monkeypatch.setattr(tv_module, "PRESET_VERIFY_TIMEOUT", 0.3)
    tv.luna_blocked = True
    with pytest.raises(CommandFailed):
        await controller.set_picture_mode("cinema")
    assert tv.model.picture_mode == "filmMaker"


async def test_empty_preset_rejected(controller):
    with pytest.raises(BadRequest):
        await controller.set_picture_mode("  ")


@pytest.mark.parametrize(
    ("label", "kind"),
    [("", "sdr"), ("hdr10", "hdr10"), ("HDR10+", "hdr10"), ("hdr10plus", "hdr10"),
     ("dolbyvision", "dolbyvision"), ("hlg", "hlg"), ("weird", None)],
)
def test_normalize_hdr_type(label, kind):
    assert normalize_hdr_type(label) == kind


def rules_settings(**kw):
    return dataclasses.replace(
        BASE, rules_enabled=True, preset_hdr10="cinema", preset_dv="", preset_sdr="",
        restore_on_stop=True, rules_debounce=0.1, **kw,
    )


async def test_hdr10_applies_preset_and_restores_idle_mode(tv, controller):
    rules = PlaybackRules(controller, rules_settings())
    assert rules.idle_mode == "filmMaker"
    rules.av_started("hdr10")
    # Real TVs switch to their HDR memory when the signal changes; the
    # restore target must stay the idle (SDR) mode, not this one.
    await tv.set_picture_mode("expert1")
    await wait_for(lambda: tv.model.picture_mode == "cinema")
    rules.stopped()
    await wait_for(lambda: tv.model.picture_mode == "filmMaker")
    await rules.close()


async def test_seek_style_restart_within_debounce_does_not_thrash(tv, controller):
    rules = PlaybackRules(controller, rules_settings())
    rules.av_started("hdr10")
    await wait_for(lambda: tv.model.picture_mode == "cinema")
    calls = len(tv.luna_calls)
    rules.stopped()
    await asyncio.sleep(0.03)
    rules.av_started("hdr10")  # quick stop/start
    await asyncio.sleep(0.3)
    assert tv.model.picture_mode == "cinema"
    assert len(tv.luna_calls) == calls  # no restore, no re-apply
    await rules.close()


async def test_no_rule_for_type_changes_nothing(tv, controller):
    rules = PlaybackRules(controller, rules_settings())
    rules.av_started("dolbyvision")  # preset_dv empty
    await asyncio.sleep(0.3)
    rules.stopped()
    await asyncio.sleep(0.3)
    assert tv.luna_calls == []
    await rules.close()


async def test_rules_disabled(tv, controller):
    rules = PlaybackRules(controller, dataclasses.replace(rules_settings(), rules_enabled=False))
    rules.av_started("hdr10")
    await asyncio.sleep(0.3)
    assert tv.luna_calls == []
    await rules.close()


async def test_no_restore_when_disabled(tv, controller):
    rules = PlaybackRules(controller, dataclasses.replace(rules_settings(), restore_on_stop=False))
    rules.av_started("hdr10")
    await wait_for(lambda: tv.model.picture_mode == "cinema")
    rules.stopped()
    await asyncio.sleep(0.3)
    assert tv.model.picture_mode == "cinema"
    await rules.close()


async def test_idle_mode_tracks_user_changes_between_sessions(tv, controller):
    rules = PlaybackRules(controller, rules_settings())
    await tv.set_picture_mode("vivid")
    await wait_for(lambda: rules.idle_mode == "vivid")
    rules.av_started("hdr10")
    await wait_for(lambda: tv.model.picture_mode == "cinema")
    rules.stopped()
    await wait_for(lambda: tv.model.picture_mode == "vivid")
    await rules.close()
