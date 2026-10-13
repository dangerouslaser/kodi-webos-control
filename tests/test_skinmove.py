"""Moving a box from skin.estuary.lg to skin.estuary.yblod."""

from __future__ import annotations

import json

import pytest

from resources.lib import skinmove


@pytest.fixture
def box(kodi, tmp_path, monkeypatch):
    """A Kodi on the old skin, at home, with the new skin installed."""
    profile = tmp_path / "profile"
    (profile / "addon_data" / skinmove.OLD).mkdir(parents=True)
    (profile / "addon_data" / skinmove.OLD / "settings.xml").write_text("<settings>old</settings>")
    monkeypatch.setattr(skinmove.xbmcvfs, "translatePath", lambda _p: str(profile) + "/")
    state = {"skin": skinmove.OLD, "dialog_after": 2}
    calls = []

    def rpc(request):
        req = json.loads(request)
        calls.append(req)
        if req["method"] == "Settings.GetSettingValue":
            return json.dumps({"result": {"value": state["skin"]}})
        if req["method"] == "Settings.SetSettingValue":
            state["skin"] = req["params"]["value"]
        return json.dumps({"result": True})

    def condition(cond):
        if cond == "Window.IsActive(yesnodialog)":
            state["dialog_after"] -= 1
            return state["dialog_after"] <= 0
        return {"System.HasAddon(skin.estuary.yblod)": state.get("installed", True),
                "System.AddonIsEnabled(skin.estuary.yblod)": state.get("installed", True)}.get(
            cond, cond.startswith("Window.IsActive(home)"))

    monkeypatch.setattr(skinmove.xbmc, "executeJSONRPC", rpc)
    monkeypatch.setattr(skinmove.xbmc, "getCondVisibility", condition)
    monkeypatch.setattr(skinmove, "SETTLE", 0.0)
    monitor = skinmove.xbmc.Monitor()
    monkeypatch.setattr(monitor, "waitForAbort", lambda _t=None: False)
    return state, calls, profile, monitor


def test_switches_copies_settings_and_says_yes(box, kodi):
    state, calls, profile, monitor = box
    skinmove.run(monitor)
    assert state["skin"] == skinmove.NEW
    assert (profile / "addon_data" / skinmove.NEW / "settings.xml").read_text() == "<settings>old</settings>"
    assert kodi.builtins == [f"SendClick(yesnodialog,{skinmove.YES_BUTTON})"]


def test_keeps_the_new_skins_own_settings(box):
    state, _calls, profile, monitor = box
    (profile / "addon_data" / skinmove.NEW).mkdir(parents=True)
    (profile / "addon_data" / skinmove.NEW / "settings.xml").write_text("<settings>new</settings>")
    skinmove.run(monitor)
    assert (profile / "addon_data" / skinmove.NEW / "settings.xml").read_text() == "<settings>new</settings>"
    assert state["skin"] == skinmove.NEW


def test_other_skins_are_left_alone(box, kodi):
    state, calls, _profile, monitor = box
    state["skin"] = "skin.estuary"
    skinmove.run(monitor)
    assert state["skin"] == "skin.estuary" and kodi.builtins == []


def test_asks_to_install_the_new_skin_once(box, kodi, monkeypatch):
    state, _calls, _profile, monitor = box
    state["installed"] = False
    monkeypatch.setattr(skinmove.storage, "read_json", lambda *_a: False)
    asked = []
    monkeypatch.setattr(skinmove.storage, "write_json", lambda name, value: asked.append(value))
    ticks = iter(range(5))
    monkeypatch.setattr(monitor, "waitForAbort", lambda _t=None: next(ticks, None) is None)
    skinmove.run(monitor)
    assert kodi.builtins == [f"InstallAddon({skinmove.NEW})"] and asked == [True]
    assert state["skin"] == skinmove.OLD          # not installed (yet): no switch
