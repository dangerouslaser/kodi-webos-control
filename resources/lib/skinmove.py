"""Move a box from the skin's old id (skin.estuary.lg, "Estuary LG") to its
new one (skin.estuary.yblod, "Estuary YBLOD"), once.

Kodi treats a renamed skin as another skin: its settings (widgets, menu
items, the setup choices) are kept per skin id, and the box keeps using the
old one until told otherwise. So, while the old skin is in use:

1. If the new skin isn't installed, ask Kodi to install it (from the
   dangerouslaser repository), once; Kodi asks you first.
2. When it is, copy the old skin's settings to the new id (unless the new
   one already has its own), switch the skin, and answer Kodi's "Keep this
   skin?" question (Kodi switches back after 10 s without a yes).

Runs on its own thread after the service starts, waits for the home screen
and for nothing to be playing, and does nothing on boxes already moved or on
other skins. Only xbmc calls here: creating an xbmcgui.Window off the
service thread crashes Kodi.
"""

from __future__ import annotations

import json
import os
import shutil

import xbmc
import xbmcvfs

from . import kodilog as log
from . import storage

OLD, NEW = "skin.estuary.lg", "skin.estuary.yblod"
ASKED_FILE = "skin_install_asked.json"
YES_BUTTON = 11  # Kodi's yes/no dialog
SETTLE = 5.0     # after the home screen shows, before switching
CONFIRM_WAIT = 8.0


def _rpc(method: str, params: dict) -> dict:
    reply = json.loads(xbmc.executeJSONRPC(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})))
    return reply if isinstance(reply, dict) else {}


def current_skin() -> str:
    result = _rpc("Settings.GetSettingValue", {"setting": "lookandfeel.skin"}).get("result") or {}
    return str(result.get("value", ""))


def installed(addon_id: str) -> bool:
    return xbmc.getCondVisibility(f"System.HasAddon({addon_id})") and \
        xbmc.getCondVisibility(f"System.AddonIsEnabled({addon_id})")


def copy_settings(profile: str) -> bool:
    """The old skin's settings to the new id, unless it has its own. True if copied."""
    old = os.path.join(profile, "addon_data", OLD, "settings.xml")
    new = os.path.join(profile, "addon_data", NEW, "settings.xml")
    if not os.path.isfile(old) or os.path.isfile(new):
        return False
    os.makedirs(os.path.dirname(new), exist_ok=True)
    shutil.copy2(old, new)
    return True


def run(monitor: xbmc.Monitor) -> None:
    try:
        _run(monitor)
    except Exception as err:  # noqa: BLE001 - never take the service down
        log.warning(f"skin move: {err!r}")


def _wait(monitor: xbmc.Monitor, condition: str, seconds: float = 0.0) -> bool:
    """Until condition holds (then `seconds` more); False if Kodi is stopping."""
    while not xbmc.getCondVisibility(condition):
        if monitor.waitForAbort(1.0):
            return False
    return not monitor.waitForAbort(seconds) if seconds else True


def _run(monitor: xbmc.Monitor) -> None:
    if current_skin() != OLD:
        return
    if not _wait(monitor, "Window.IsActive(home) + !Player.HasMedia", SETTLE):
        return
    if current_skin() != OLD:
        return
    if not installed(NEW):
        if storage.read_json(ASKED_FILE, False):
            return  # asked once already; installing it is up to you now
        storage.write_json(ASKED_FILE, True)
        log.info(f"skin move: asking Kodi to install {NEW}")
        xbmc.executebuiltin(f"InstallAddon({NEW})")
        # Kodi asks first; once it's installed, carry on.
        for _ in range(300):
            if installed(NEW):
                break
            if monitor.waitForAbort(1.0):
                return
        else:
            return
        if not _wait(monitor, "Window.IsActive(home) + !Player.HasMedia + !System.HasModalDialog", 2.0):
            return
    if copy_settings(xbmcvfs.translatePath("special://profile/")):
        log.info(f"skin move: copied {OLD}'s settings to {NEW}")
    log.info(f"skin move: switching to {NEW}")
    _rpc("Settings.SetSettingValue", {"setting": "lookandfeel.skin", "value": NEW})
    waited = 0.0
    while waited < CONFIRM_WAIT:
        if xbmc.getCondVisibility("Window.IsActive(yesnodialog)"):
            xbmc.executebuiltin(f"SendClick(yesnodialog,{YES_BUTTON})")
            log.info(f"skin move: now on {NEW}")
            return
        if monitor.waitForAbort(0.25):
            return
        waited += 0.25
    log.warning("skin move: Kodi didn't ask to keep the new skin; check Settings > Interface > Skin")
