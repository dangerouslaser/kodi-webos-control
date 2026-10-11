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


def test_preset_grouping_and_labels():
    from resources.lib import presets

    values = ["filmMaker", "cinema", "hdrCinema", "hdrCinemaBright", "dolbyHdrCinema", "expert2"]
    assert presets.for_kind("sdr", values) == ["filmMaker", "cinema", "expert2"]
    assert presets.for_kind("hdr10", values) == ["hdrCinema", "hdrCinemaBright"]
    assert presets.for_kind("hlg", values) == ["hdrCinema", "hdrCinemaBright"]
    assert presets.for_kind("dolbyvision", values) == ["dolbyHdrCinema"]
    assert presets.label("filmMaker") == "Filmmaker Mode"
    assert presets.label("hdrCinemaBright") == "Cinema Home (HDR)"
    assert presets.label("dolbyHdrCinema") == "Cinema (Dolby Vision)"
    assert presets.label("expert2") == "Expert (Dark room)"
    assert presets.label("dolbyHdrSomethingNew") == "Something new (Dolby Vision)"


async def test_picture_modes_from_tv(tv, controller):
    modes = await controller.picture_modes()
    values = {m["value"]: m["visible"] for m in modes}
    assert values["filmMaker"] is True
    assert values["hdrCinema"] is False


async def test_idle_mode_ignores_hdr_and_dv_presets(tv, controller):
    """After playback the TV can briefly report its DV memory; never restore to it."""
    rules = PlaybackRules(controller, rules_settings())
    assert rules.idle_mode == "filmMaker"
    await tv.set_picture_mode("dolbyHdrCinemaBright")
    await wait_for(lambda: controller.picture_preset == "dolbyHdrCinemaBright")
    await asyncio.sleep(0.05)
    assert rules.idle_mode == "filmMaker"
    await tv.set_picture_mode("vivid")
    await wait_for(lambda: rules.idle_mode == "vivid")
    await rules.close()


async def test_apply_waits_for_the_signal_to_switch(tv, controller, monkeypatch):
    import resources.lib.automations as automations

    monkeypatch.setattr(automations, "SIGNAL_SETTLE", 3.0)
    rules = PlaybackRules(controller, dataclasses.replace(rules_settings(), preset_hdr10="hdrCinema"))
    rules.av_started("hdr10")
    await asyncio.sleep(0.5)
    assert tv.luna_calls == []  # TV still on SDR: don't try an HDR preset yet
    await tv.set_picture_mode("hdrStandard")  # the TV switched to its HDR memory
    await wait_for(lambda: tv.model.picture_mode == "hdrCinema")
    rules.stopped()
    # Back on SDR: restore the SDR idle preset once the TV reports SDR again.
    await tv.set_picture_mode("normal")
    await wait_for(lambda: tv.model.picture_mode == "filmMaker")
    await rules.close()


async def test_apply_retries_when_the_tv_refuses(tv, controller, monkeypatch):
    rules = PlaybackRules(controller, rules_settings())
    tv.luna_blocked = True
    rules.av_started("hdr10")  # preset "cinema"

    async def unblock():
        await asyncio.sleep(0.3)
        tv.luna_blocked = False

    await asyncio.gather(unblock(), wait_for(lambda: tv.model.picture_mode == "cinema", timeout=8))
    await rules.close()


async def test_stop_while_waiting_for_signal_gives_up_quickly(tv, controller, monkeypatch):
    import resources.lib.automations as automations

    monkeypatch.setattr(automations, "SIGNAL_SETTLE", 5.0)
    rules = PlaybackRules(controller, dataclasses.replace(rules_settings(), preset_hdr10="hdrCinema"))
    rules.av_started("hdr10")
    await asyncio.sleep(0.3)
    rules.stopped()
    await asyncio.sleep(0.6)
    assert tv.luna_calls == []
    assert not rules._session
    await rules.close()
