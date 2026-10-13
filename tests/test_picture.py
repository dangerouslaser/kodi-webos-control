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


@pytest.fixture
async def old_webos_controller(patch_ports):
    """A TV like webOS 5.5: picture mode can be set, but not read or listed."""
    tv = FakeTv()
    tv.fail_uris["settings/getSystemSettings"] = "Some keys are not allowed for the request. ( pictureMode )"
    tv.fail_uris["settings/getSystemSettingDesc"] = "category, picture doesn't support the key(s): pictureMode"
    await tv.start()
    patch_ports(tv)
    publisher = PropertyPublisher()
    c = TvController(BASE, on_state=publisher.publish, backoff_base=0.05)
    await c.start()
    await wait_for(lambda: c.connected and c.picture_readable is False)
    yield tv, c
    await c.stop()
    await tv.stop()


async def test_old_webos_sets_presets_blind(old_webos_controller):
    tv, controller = old_webos_controller
    home = xbmcgui.Window(10000)
    await wait_for(lambda: home.getProperty("WebOS.PictureReadable") == "false")
    assert home.getProperty("WebOS.PictureSupported") == "true"
    modes = [m["value"] for m in await controller.picture_modes()]
    assert "filmMaker" in modes and "hdrCinema" in modes and "dolbyHdrCinema" in modes
    # Sent once, no read-back, and remembered as the current preset.
    assert await controller.set_picture_mode("vivid") == "vivid"
    assert tv.model.picture_mode == "vivid" and len(tv.luna_calls) == 1
    await wait_for(lambda: home.getProperty("WebOS.PicturePresetLabel") == "Vivid")
    assert await controller.get_picture_mode() == "vivid"
    import xbmc

    warnings = [m for level, m in xbmc.logs if level >= xbmc.LOGWARNING and "picture" in m.lower()]
    assert warnings == []


async def test_old_webos_rules_apply_after_a_fixed_wait(old_webos_controller, monkeypatch):
    import resources.lib.automations as automations

    monkeypatch.setattr(automations, "BLIND_SIGNAL_DELAY", 0.2)
    tv, controller = old_webos_controller
    rules = PlaybackRules(controller, dataclasses.replace(rules_settings(), preset_hdr10="hdrCinema"))
    assert rules.idle_mode is None  # unknown: the TV won't say
    rules.av_started("hdr10")
    await wait_for(lambda: tv.model.picture_mode == "hdrCinema")
    calls = len(tv.luna_calls)
    rules.stopped()
    await asyncio.sleep(0.3)
    # Nothing to restore: the TV keeps HDR and SDR presets apart, and the SDR one is unknown.
    assert len(tv.luna_calls) == calls
    await rules.close()


async def test_picture_readable_flag_on_new_webos(tv, controller):
    assert controller.picture_readable is True
    await wait_for(lambda: xbmcgui.Window(10000).getProperty("WebOS.PictureReadable") == "true")


def test_only_one_service_instance(tmp_path, monkeypatch):
    from resources.lib import service as service_mod

    s1, s2 = service_mod.WebOsService(), service_mod.WebOsService()
    assert s1._acquire_instance_lock()
    assert not s2._acquire_instance_lock()
    s1._release_instance_lock()
    assert s2._acquire_instance_lock()
    s2._release_instance_lock()


# motion smoothing ---------------------------------------------------------


@pytest.mark.parametrize(("fps", "cls"), [(23.976, "film"), (25, "film"), (30, "film"),
                                          (50, "video"), (59.94, "video"), (0, None)])
def test_motion_class(fps, cls):
    from resources.lib.automations import motion_class

    assert motion_class(fps) == cls


async def test_motion_follows_frame_rate_after_the_preset(tv, controller):
    settings = rules_settings(motion_film="off", motion_video="smooth", motion_restore="natural")
    rules = PlaybackRules(controller, settings)
    rules.av_started("hdr10", 23.976)
    # Sent after the preset, so it lands in that preset's memory.
    await wait_for(lambda: tv.model.motion.get("cinema") == "off")
    assert controller.status()["motion"] == "off"
    rules.stopped()
    await wait_for(lambda: tv.model.picture_mode == "filmMaker")
    await wait_for(lambda: tv.model.motion.get("filmMaker") == "natural")
    await rules.close()


async def test_motion_without_picture_rules(tv, controller):
    settings = dataclasses.replace(BASE, motion_video="smooth", rules_debounce=0.1)
    rules = PlaybackRules(controller, settings)
    rules.av_started("sdr", 59.94)
    await wait_for(lambda: tv.model.motion.get("filmMaker") == "smooth")
    calls = len(tv.luna_calls)
    rules.stopped()  # no "after playback" value: nothing sent
    await asyncio.sleep(0.3)
    assert len(tv.luna_calls) == calls
    await rules.close()


async def test_motion_unknown_frame_rate_sends_nothing(tv, controller):
    settings = dataclasses.replace(BASE, motion_film="off", motion_video="smooth",
                                   motion_restore="natural", rules_debounce=0.1)
    rules = PlaybackRules(controller, settings)
    rules.av_started("sdr", 0)
    rules.stopped()
    await asyncio.sleep(0.4)
    assert tv.luna_calls == []
    await rules.close()


# sound output -------------------------------------------------------------


def sound_settings(**kw):
    return dataclasses.replace(BASE, rules_debounce=0.1, **kw)


async def test_sound_output_by_content_and_restore(tv, controller):
    from resources.lib.soundrules import SoundRules

    sound = SoundRules(controller, sound_settings(sound_music="external_arc", sound_livetv="tv_speaker"))
    sound.started("music")
    await wait_for(lambda: tv.model.sound_output == "external_arc")
    sound.started("video")  # no rule for video: back to the user's output
    await wait_for(lambda: tv.model.sound_output == "tv_speaker")
    sound.started("music")
    await wait_for(lambda: tv.model.sound_output == "external_arc")
    sound.stopped()
    await wait_for(lambda: tv.model.sound_output == "tv_speaker")
    await sound.close()


async def test_sound_output_quick_restart_does_not_switch_back(tv, controller):
    from resources.lib.soundrules import SoundRules

    sound = SoundRules(controller, sound_settings(sound_music="external_arc"))
    sound.started("music")
    await wait_for(lambda: controller.state.sound_output == "external_arc")
    sound.stopped()
    sound.started("music")  # next playlist item
    await asyncio.sleep(0.3)
    assert tv.model.sound_output == "external_arc"
    await sound.close()


async def test_sound_output_changed_by_user_is_kept(tv, controller):
    from resources.lib.soundrules import SoundRules

    sound = SoundRules(controller, sound_settings(sound_video="external_arc"))
    sound.started("video")
    await wait_for(lambda: controller.state.sound_output == "external_arc")
    await controller.sound_output("bt_soundbar")  # picked during playback
    await wait_for(lambda: controller.state.sound_output == "bt_soundbar")
    sound.stopped()
    await asyncio.sleep(0.3)
    assert tv.model.sound_output == "bt_soundbar"
    await sound.close()


# picture settings and source quality -------------------------------------


@pytest.mark.parametrize(("content", "height", "cls"), [
    ("livetv", 0, "livetv"), ("livetv", 2160, "livetv"), ("video", 576, "sd"),
    ("video", 720, "hd"), ("video", 1080, "hd"), ("video", 1600, "uhd"), ("video", 2160, "uhd"),
    ("video", 0, None),
])
def test_source_class(content, height, cls):
    from resources.lib.adjustments import source_class

    assert source_class(content, height) == cls


async def test_picture_setting_read_and_write(tv, controller):
    values = await controller.picture_settings(["backlight", "noiseReduction", "bogus"])
    assert values == {"backlight": "80", "noiseReduction": "off"}
    assert await controller.set_picture_setting("noiseReduction", "MEDIUM") == "medium"
    assert tv.model.picture["filmMaker"] == {"noiseReduction": "medium"}
    assert await controller.set_picture_setting("backlight", "60") == "60"
    assert tv.model.picture["filmMaker"]["backlight"] == 60  # sent as a number
    assert (await controller.picture_settings(["backlight"]))["backlight"] == "60"
    with pytest.raises(BadRequest):
        await controller.set_picture_setting("noiseReduction", "extreme")
    with pytest.raises(BadRequest):
        await controller.set_picture_setting("backlight", "101")
    with pytest.raises(BadRequest):
        await controller.set_picture_setting("sharpness", "10")


async def test_old_webos_picture_settings_are_remembered(old_webos_controller):
    tv, controller = old_webos_controller
    assert await controller.picture_settings(["eyeComfortMode"]) == {"eyeComfortMode": ""}
    await controller.set_picture_setting("eyeComfortMode", "on")
    assert tv.model.picture["filmMaker"]["eyeComfortMode"] == "on"
    assert await controller.picture_settings(["eyeComfortMode"]) == {"eyeComfortMode": "on"}


QUALITY = ("noiseReduction", "mpegNoiseReduction", "smoothGradation", "superResolution")


async def test_source_quality_applies_and_restores(tv, controller):
    tv.model.picture["filmMaker"] = {"noiseReduction": "low"}
    settings = dataclasses.replace(BASE, quality_livetv="high", quality_hd="off", rules_debounce=0.1)
    rules = PlaybackRules(controller, settings)
    rules.av_started("sdr", 50, "livetv", 1080)
    await wait_for(lambda: tv.model.picture["filmMaker"].get("superResolution") == "high")
    assert {k: tv.model.picture["filmMaker"][k] for k in QUALITY} == dict.fromkeys(QUALITY, "high")
    rules.stopped()
    # Same picture mode as before: the previous values come back.
    await wait_for(lambda: tv.model.picture["filmMaker"]["noiseReduction"] == "low")
    assert tv.model.picture["filmMaker"]["superResolution"] == "off"
    await rules.close()


async def test_source_quality_in_another_mode_is_not_restored(tv, controller):
    settings = rules_settings(quality_hd="medium")
    rules = PlaybackRules(controller, settings)
    rules.av_started("hdr10", 23.976, "video", 2160)
    await wait_for(lambda: tv.model.picture.get("cinema", {}).get("noiseReduction") == "medium")
    rules.stopped()
    await wait_for(lambda: tv.model.picture_mode == "filmMaker")
    await asyncio.sleep(0.2)
    assert "filmMaker" not in tv.model.picture  # the menus' mode was never touched
    assert tv.model.picture["cinema"]["noiseReduction"] == "medium"
    await rules.close()


async def test_source_quality_unknown_source_does_nothing(tv, controller):
    settings = dataclasses.replace(BASE, quality_sd="high", quality_hd="high", rules_debounce=0.1)
    rules = PlaybackRules(controller, settings)
    rules.av_started("sdr", 24, "video", 0)
    rules.stopped()
    await asyncio.sleep(0.4)
    assert tv.luna_calls == []
    await rules.close()


async def test_4k_follows_hd_unless_set_and_custom_picks_cleanups(tv, controller):
    custom = {"uhd": {"noiseReduction": "off", "mpegNoiseReduction": "", "smoothGradation": "low",
                      "superResolution": ""}}
    settings = dataclasses.replace(BASE, quality_hd="medium", quality_uhd="custom", quality_custom=custom,
                                   rules_debounce=0.1)
    rules = PlaybackRules(controller, settings)
    rules.av_started("sdr", 24, "video", 1600)  # a wide 4K film
    await wait_for(lambda: tv.model.picture.get("filmMaker", {}).get("smoothGradation") == "low")
    applied = tv.model.picture["filmMaker"]
    assert applied["noiseReduction"] == "off"
    assert "superResolution" not in applied and "mpegNoiseReduction" not in applied  # left unchanged
    rules.stopped()
    await rules.close()

    settings = dataclasses.replace(BASE, quality_hd="medium", rules_debounce=0.1)  # 4K: same as HD
    rules = PlaybackRules(controller, settings)
    rules.av_started("sdr", 24, "video", 2160)
    await wait_for(lambda: tv.model.picture["filmMaker"].get("superResolution") == "medium")
    await rules.close()


async def test_cleanup_after_playback_sets_a_level(tv, controller):
    tv.model.picture["filmMaker"] = dict.fromkeys(QUALITY, "low")
    settings = dataclasses.replace(BASE, quality_livetv="high", quality_restore="off", rules_debounce=0.1)
    rules = PlaybackRules(controller, settings)
    rules.av_started("sdr", 50, "livetv", 1080)
    await wait_for(lambda: tv.model.picture["filmMaker"]["superResolution"] == "high")
    rules.stopped()
    # Not back to "low": the after-playback level.
    await wait_for(lambda: tv.model.picture["filmMaker"]["noiseReduction"] == "off")
    assert {k: tv.model.picture["filmMaker"][k] for k in QUALITY} == dict.fromkeys(QUALITY, "off")
    await rules.close()


def test_quality_values():
    from resources.lib.automations import describe_quality, quality_values

    assert quality_values("high", None) == dict.fromkeys(QUALITY, "high")
    assert quality_values("", {"noiseReduction": "off"}) == {}
    assert quality_values("custom", None) == {}
    picked = quality_values("custom", {"noiseReduction": "auto", "superResolution": ""})
    assert picked == {"noiseReduction": "auto"}
    assert describe_quality(dict.fromkeys(QUALITY, "low")) == "low"
    assert describe_quality(picked) == "noiseReduction=auto"
