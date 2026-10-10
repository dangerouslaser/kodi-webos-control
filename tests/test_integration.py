"""TV integration: TV-off / switch-away reactions, transparent control,
one-shot playback overrides (context menu)."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
import xbmcgui
from fake_tv import FakeTv

from resources.lib import actions, presets, remote, watcher
from resources.lib.automations import PlaybackRules
from resources.lib.errors import BadRequest
from resources.lib.settings import Settings
from resources.lib.state import TvState
from resources.lib.tv import TvController

BASE = Settings(host="127.0.0.1", loopback_fallback=False, auto_discover=False)
INPUTS = (("HDMI_1", "PC", "com.webos.app.hdmi1"), ("HDMI_2", "Sonos", "com.webos.app.hdmi2"))


async def wait_for(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class FakeKodi:
    def __init__(self):
        self.playing = False
        self.paused = False
        self.calls: list[str] = []
        self.overlay: list[bool] = []
        self.saved: dict[str, str] = {}

    def is_playing(self):
        return self.playing and not self.paused

    def is_paused(self):
        return self.playing and self.paused

    def pause(self):
        self.calls.append("pause")
        self.paused = True

    def resume(self):
        self.calls.append("resume")
        self.paused = False

    def stop(self):
        self.calls.append("stop")
        self.playing = self.paused = False

    def shutdown(self):
        self.calls.append("shutdown")

    def set_overlay(self, active):
        self.overlay.append(active)

    def save_setting(self, key, value):
        self.saved[key] = value


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch):
    monkeypatch.setattr(watcher, "DROP_GRACE", 0.3)
    monkeypatch.setattr(watcher, "SHUTDOWN_DELAY", 0.3)
    monkeypatch.setattr(watcher, "FOREGROUND_SETTLE", 0.05)


@pytest.fixture
async def tv(patch_ports):
    tv = FakeTv()
    await tv.start()
    patch_ports(tv)
    yield tv
    await tv.stop()


@pytest.fixture
async def controller(tv):
    c = TvController(BASE, backoff_base=0.05)
    await c.start()
    await wait_for(lambda: c.connected and c.picture_preset)
    yield c
    await c.stop()


@pytest.fixture
def kodi():
    return FakeKodi()


async def make_watcher(controller, kodi, **kw):
    w = watcher.TvWatcher(controller, dataclasses.replace(BASE, **kw), kodi)
    controller._publish()  # feed the current state
    await asyncio.sleep(0.1)
    return w


# foreground detection ------------------------------------------------------


def state(app_id, **kw):
    return TvState(connected=True, is_on=True, app_id=app_id, inputs=INPUTS,
                   apps=(("org.xbmc.kodi", "Kodi"),), **kw)


def test_kodi_foreground_by_input_setting():
    s = dataclasses.replace(BASE, host="192.0.2.5", kodi_input="HDMI_1")
    assert watcher.kodi_foreground(s, state("com.webos.app.hdmi1")) is True
    assert watcher.kodi_foreground(s, state("netflix")) is False
    # by label too
    s = dataclasses.replace(s, kodi_input="pc")
    assert watcher.kodi_foreground(s, state("com.webos.app.hdmi1")) is True


def test_kodi_foreground_unknown_cases():
    off_tv = dataclasses.replace(BASE, host="192.0.2.5")
    assert watcher.kodi_foreground(off_tv, state("netflix")) is None  # input not known yet
    s = dataclasses.replace(off_tv, kodi_input="HDMI_1")
    assert watcher.kodi_foreground(s, state("")) is None
    assert watcher.kodi_foreground(s, dataclasses.replace(state("netflix"), is_on=False)) is None


def test_kodi_foreground_on_tv_mode():
    assert watcher.kodi_foreground(BASE, state("org.xbmc.kodi")) is True
    assert watcher.kodi_foreground(BASE, state("netflix")) is False


# TV turned off --------------------------------------------------------------


async def test_tv_off_stops_playback(tv, controller, kodi):
    w = await make_watcher(controller, kodi, on_tv_off="stop")
    kodi.playing = True
    await tv.set_power("Active Standby")
    await wait_for(lambda: kodi.calls == ["stop"])
    await w.close()


async def test_tv_off_pause(tv, controller, kodi):
    w = await make_watcher(controller, kodi, on_tv_off="pause")
    kodi.playing = True
    await tv.set_power("Suspend")
    await wait_for(lambda: kodi.calls == ["pause"])
    await w.close()


async def test_screen_off_is_not_tv_off(tv, controller, kodi):
    w = await make_watcher(controller, kodi, on_tv_off="stop")
    kodi.playing = True
    await tv.set_power("Screen Off")
    await asyncio.sleep(0.2)
    assert kodi.calls == []
    await w.close()


async def test_shutdown_waits_and_is_cancelled_if_tv_returns(tv, controller, kodi):
    w = await make_watcher(controller, kodi, on_tv_off="shutdown")
    await tv.set_power("Active Standby")
    await asyncio.sleep(0.1)
    await tv.set_power("Active")
    await asyncio.sleep(0.5)
    assert kodi.calls == []
    await tv.set_power("Active Standby")
    await wait_for(lambda: kodi.calls == ["shutdown"])
    await w.close()


async def test_connection_drop_counts_as_off_only_after_grace(tv, controller, kodi):
    w = await make_watcher(controller, kodi, on_tv_off="stop")
    kodi.playing = True
    await tv.stop()  # TV gone (fully off, no Quick Start)
    await wait_for(lambda: kodi.calls == ["stop"], timeout=3)
    await w.close()


async def test_no_action_at_startup_when_tv_already_off(tv, controller, kodi):
    await tv.set_power("Active Standby")
    await wait_for(lambda: not controller.state.is_on)
    w = await make_watcher(controller, kodi, on_tv_off="shutdown")
    await asyncio.sleep(0.5)
    assert kodi.calls == []
    await w.close()


# switching away -------------------------------------------------------------


async def test_switch_away_pauses_and_resumes(tv, controller, kodi):
    w = await make_watcher(controller, kodi, on_switch_away="pause", resume_on_return=True)
    kodi.playing = True
    await tv.set_app("netflix")
    await wait_for(lambda: kodi.calls == ["pause"])
    await tv.set_app("org.xbmc.kodi")
    await wait_for(lambda: kodi.calls == ["pause", "resume"])
    await w.close()


async def test_no_resume_if_user_paused(tv, controller, kodi):
    w = await make_watcher(controller, kodi, on_switch_away="pause", resume_on_return=True)
    kodi.playing = kodi.paused = True  # already paused by the user
    await tv.set_app("netflix")
    await asyncio.sleep(0.2)
    await tv.set_app("org.xbmc.kodi")
    await asyncio.sleep(0.2)
    assert kodi.calls == []
    await w.close()


async def test_switch_away_stop(tv, controller, kodi):
    w = await make_watcher(controller, kodi, on_switch_away="stop")
    kodi.playing = True
    await tv.set_app("com.webos.app.hdmi2")
    await wait_for(lambda: kodi.calls == ["stop"])
    await w.close()


async def test_quick_flip_back_does_nothing(tv, controller, kodi, monkeypatch):
    monkeypatch.setattr(watcher, "FOREGROUND_SETTLE", 0.3)
    w = await make_watcher(controller, kodi, on_switch_away="pause")
    kodi.playing = True
    await tv.set_app("netflix")
    await asyncio.sleep(0.05)
    await tv.set_app("org.xbmc.kodi")
    await asyncio.sleep(0.5)
    assert kodi.calls == []
    await w.close()


async def test_learns_kodi_input_on_first_video(tv, controller, kodi):
    controller.settings = dataclasses.replace(controller.settings, host="192.0.2.5")
    await tv.set_app("com.webos.app.hdmi1")
    await wait_for(lambda: controller.state.input_id == "HDMI_1")
    w = watcher.TvWatcher(controller, dataclasses.replace(BASE, host="192.0.2.5"), kodi)
    w.video_started()
    assert kodi.saved == {"kodi_input": "HDMI_1"}
    await w.close()


# transparent control --------------------------------------------------------


async def test_overlay_follows_foreground(tv, controller, kodi):
    w = await make_watcher(controller, kodi, transparent_control=True)
    assert kodi.overlay == []  # Kodi on screen
    await tv.set_app("netflix")
    await wait_for(lambda: kodi.overlay == [True])
    await tv.set_app("org.xbmc.kodi")
    await wait_for(lambda: kodi.overlay == [True, False])
    await tv.set_app("netflix")
    await wait_for(lambda: kodi.overlay == [True, False, True])
    await tv.set_power("Active Standby")  # nothing to control while off
    await wait_for(lambda: kodi.overlay[-1] is False)
    await w.close()


async def test_overlay_disabled_by_setting(tv, controller, kodi):
    w = await make_watcher(controller, kodi, transparent_control=False)
    await tv.set_app("netflix")
    await asyncio.sleep(0.2)
    assert kodi.overlay == []
    await w.close()


def test_translate_kodi_actions():
    assert remote.translate(3, "HDMI_1") == ("button", ["UP"])
    assert remote.translate(7, "HDMI_1") == ("button", ["ENTER"])
    assert remote.translate(92, "HDMI_1") == ("button", ["BACK"])
    assert remote.translate(88, "HDMI_1") == ("volume", ["up"])
    assert remote.translate(91, "HDMI_1") == ("mute", ["toggle"])
    assert remote.translate(63, "HDMI_1") == ("button", ["5"])
    assert remote.translate(117, "HDMI_1") == ("input", ["HDMI_1"])
    assert remote.translate(117, "") is None
    assert remote.translate(107, "HDMI_1") is None  # mouse move


def test_every_translated_button_exists():
    from aiowebostv.buttons import BUTTONS

    for action_id in range(300):
        mapped = remote.translate(action_id, "HDMI_1")
        if mapped and mapped[0] == "button":
            assert mapped[1][0] in BUTTONS


def test_overlay_forwards_presses():
    sent = []
    overlay = remote.Overlay(lambda name, args: sent.append((name, args)), lambda: "HDMI_1")
    overlay.open()
    window = overlay._window
    assert window.shown.wait(2)
    window.press(4)
    window.press(229)
    window.press(229)
    window.press(117)
    assert sent == [("button", ["DOWN"]), ("button", ["PAUSE"]), ("button", ["PLAY"]),
                    ("input", ["HDMI_1"])]
    overlay.close()
    overlay._thread.join(2)
    assert not overlay.active
    assert xbmcgui.modal_windows == []


# one-shot overrides -----------------------------------------------------------


def test_parse_next_playback():
    _a, kw = actions.parse("next_playback", ["picture=filmmaker", "screen_off"])
    assert kw["override"] == {"picture": "filmmaker", "screen_off": True, "power_off_at_end": False}
    _a, kw = actions.parse("next_playback", [{"power_off_at_end": True}])
    assert kw["override"]["power_off_at_end"] is True
    with pytest.raises(BadRequest):
        actions.parse("next_playback", [])
    with pytest.raises(BadRequest):
        actions.parse("next_playback", ["explode"])


def test_resolve_intents():
    modes = [{"value": v, "visible": v.startswith("hdr")} for v in
             ("filmMaker", "cinema", "hdrFilmMaker", "hdrCinema", "dolbyHdrCinema")]
    fm = presets.candidates("filmmaker")
    assert presets.resolve(fm, "sdr", modes) == "filmMaker"
    assert presets.resolve(fm, "hdr10", modes) == "hdrFilmMaker"
    assert presets.resolve(fm, "dolbyvision", modes) == "dolbyHdrCinema"  # no DV filmmaker
    assert presets.resolve(fm, None, modes) == "hdrFilmMaker"  # visible = current signal
    # A preset picked for the wrong signal maps to its equivalent.
    assert presets.resolve(presets.candidates("hdrCinema"), "sdr", modes) == "cinema"
    assert presets.equivalents("normal") == ["normal", "hdrStandard", "dolbyHdrStandard"]


async def test_override_filmmaker_for_hdr_and_restore(tv, controller):
    rules = PlaybackRules(controller, dataclasses.replace(BASE, rules_debounce=0.05))
    rules.arm({"picture": "filmmaker", "screen_off": False, "power_off_at_end": False})
    await tv.set_picture_mode("vivid")
    await wait_for(lambda: rules.idle_mode == "vivid")
    rules.av_started("hdr10")  # rules disabled, override still applies
    await wait_for(lambda: tv.model.picture_mode == "hdrFilmMaker")
    rules.stopped()
    await wait_for(lambda: tv.model.picture_mode == "vivid")
    # One-shot: the next video uses the (disabled) rules again.
    calls = len(tv.luna_calls)
    rules.av_started("hdr10")
    await asyncio.sleep(0.2)
    assert len(tv.luna_calls) == calls
    await rules.close()


async def test_override_screen_off_and_power_off_at_end(tv, controller):
    rules = PlaybackRules(controller, dataclasses.replace(BASE, rules_debounce=0.05))
    import resources.lib.automations as automations

    automations.END_SETTLE, saved = 0.1, automations.END_SETTLE
    try:
        rules.arm({"picture": "", "screen_off": True, "power_off_at_end": True})
        rules.av_started("sdr")
        await wait_for(lambda: tv.model.power == "Screen Off")
        rules.ended()
        await wait_for(lambda: tv.model.power == "Suspend")
    finally:
        automations.END_SETTLE = saved
    await rules.close()


async def test_stopping_does_not_power_off(tv, controller):
    rules = PlaybackRules(controller, dataclasses.replace(BASE, rules_debounce=0.05))
    rules.arm({"picture": "", "screen_off": True, "power_off_at_end": True})
    rules.av_started("sdr")
    await wait_for(lambda: tv.model.power == "Screen Off")
    rules.stopped()
    await wait_for(lambda: tv.model.power == "Active")
    await asyncio.sleep(0.2)
    assert tv.model.power == "Active"
    await rules.close()


async def test_armed_override_expires(tv, controller, monkeypatch):
    import resources.lib.automations as automations

    monkeypatch.setattr(automations, "ARM_TTL", 0.05)
    rules = PlaybackRules(controller, dataclasses.replace(BASE, rules_debounce=0.05))
    rules.arm({"picture": "filmmaker", "screen_off": True, "power_off_at_end": False})
    await asyncio.sleep(0.1)
    rules.av_started("sdr")
    await asyncio.sleep(0.2)
    assert tv.model.power == "Active"
    await rules.close()


async def test_learns_input_then_transparent_control_starts(tv, controller, kodi):
    """Enabling the setting in Kodi's UI (TV on Kodi) learns the input."""
    off_tv = dataclasses.replace(BASE, host="192.0.2.5", transparent_control=True)
    controller.settings = dataclasses.replace(controller.settings, host="192.0.2.5")
    await tv.set_app("com.webos.app.hdmi1")
    await wait_for(lambda: controller.state.input_id == "HDMI_1")
    w = watcher.TvWatcher(controller, off_tv, kodi)
    w.update(off_tv)
    assert w.status()["kodi_on_screen"] is None  # input unknown: nothing happens
    w.learn_input("settings changed")
    assert kodi.saved == {"kodi_input": "HDMI_1"}
    await tv.set_app("com.webos.app.hdmi2")
    await wait_for(lambda: kodi.overlay == [True])
    assert w.status()["transparent_active"] is True
    await w.close()


async def test_stop_during_apply_still_restores(tv, controller, monkeypatch):
    rules = PlaybackRules(controller, dataclasses.replace(BASE, rules_debounce=0.05))
    real = controller.get_picture_mode

    async def slow_readback():
        await asyncio.sleep(0.3)  # TV slow to confirm the change
        return await real()

    monkeypatch.setattr(controller, "get_picture_mode", slow_readback)
    rules.arm({"picture": "filmmaker", "screen_off": False, "power_off_at_end": False})
    rules.av_started("hdr10")
    await wait_for(lambda: tv.model.picture_mode == "hdrFilmMaker")
    rules.stopped()  # while the apply is still verifying
    await wait_for(lambda: tv.model.picture_mode == "filmMaker")
    await rules.close()
