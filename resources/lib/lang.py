"""Localized strings (ids live in resources/language/*/strings.po)."""

from __future__ import annotations

import xbmcaddon

ADDON_ID = "service.webostv"

INPUTS = 32100
APPS = 32101
SOUND_OUTPUT = 32102
BUTTONS = 32103
SEND_TOAST = 32104
STATUS = 32105
REPAIR = 32106
TOAST_PROMPT = 32107
REPAIR_STARTED = 32108
NOT_RUNNING = 32109
CURRENT = 32110
COMMAND_RESULT = 32111
ACTION_FAILED = 32112
FIND_TV = 32113
SEARCHING = 32114
NO_TVS_FOUND = 32115
TV_SELECTED = 32116
PICTURE_PRESET = 32117
NO_CHANGE = 32118
RESUME_FROM = 32306
FROM_START = 32307
KODI_INPUT = 32308
AUTOMATIC = 32309
VOLUME = 32310
MOTION = 32430
LG_TV = 32431
SCREEN_OFF = 32432
BRIDGE_SETUP = 32602
BRIDGE_NO_HBCHANNEL = 32610
BRIDGE_INSTALL_PROMPT = 32611
BRIDGE_WAITING = 32612
BRIDGE_INSTALL_TIMEOUT = 32613
BRIDGE_SETTING_UP = 32614
BRIDGE_CONNECTED = 32615
BRIDGE_NOT_CONNECTED = 32616
BRIDGE_REMOVED = 32617
BRIDGE_UPDATE_PROMPT = 32619
BRIDGE_UPDATING = 32623
BRIDGE_UPDATED = 32624
BRIDGE_UPDATE_TIMEOUT = 32625
BRIDGE_UPDATE_REPO = 32626
BRIDGE_UPDATE_AVAILABLE = 32627
TV_NOT_CONNECTED = 32618
MOTION_LABELS = {"off": 32411, "cinemaClear": 32412, "natural": 32413, "smooth": 32414}

_FALLBACK = {
    INPUTS: "Inputs",
    APPS: "Apps",
    SOUND_OUTPUT: "Sound output",
    BUTTONS: "Remote buttons",
    SEND_TOAST: "Send toast",
    STATUS: "Status",
    REPAIR: "Re-pair",
    TOAST_PROMPT: "Message to show on the TV",
    REPAIR_STARTED: "Accept the pairing prompt on the TV",
    NOT_RUNNING: "The webOS service is not responding",
    CURRENT: "current",
    COMMAND_RESULT: "SSAP response",
    ACTION_FAILED: "Action failed",
    FIND_TV: "Find TV",
    SEARCHING: "Searching the network for LG TVs...",
    NO_TVS_FOUND: "No LG webOS TVs found on the network",
    TV_SELECTED: "TV selected",
    PICTURE_PRESET: "Picture preset",
    NO_CHANGE: "No change",
    RESUME_FROM: "Resume from %s",
    FROM_START: "Play from beginning",
    KODI_INPUT: "Kodi's TV input",
    AUTOMATIC: "Automatic",
    VOLUME: "Volume",
    MOTION: "Motion smoothing",
    LG_TV: "LG TV",
    SCREEN_OFF: "Screen off",
    BRIDGE_SETUP: "Set up Remote Bridge on the TV",
    BRIDGE_NO_HBCHANNEL: "Remote Bridge needs a rooted TV with Homebrew Channel installed. "
                         "Homebrew Channel wasn't found on this TV.",
    BRIDGE_INSTALL_PROMPT: "The Remote Bridge app isn't on the TV yet. Kodi will open Homebrew Channel "
                           "on the TV with the Remote Bridge repository filled in. On the TV: confirm "
                           "adding the repository, then open Remote Bridge for Kodi in the app list "
                           "and install it. Kodi finishes the setup by itself and brings you back here.",
    BRIDGE_WAITING: "Waiting for Remote Bridge to be installed on the TV...",
    BRIDGE_INSTALL_TIMEOUT: "Remote Bridge wasn't installed within 10 minutes. "
                            "Run Set up again once it's installed.",
    BRIDGE_SETTING_UP: "Setting up Remote Bridge on the TV...",
    BRIDGE_CONNECTED: "Remote Bridge connected: the TV remote now controls Kodi.",
    BRIDGE_NOT_CONNECTED: "The Remote Bridge app was set up, but it hasn't connected to Kodi. Check that "
                          "the TV can reach this Kodi on TCP port %s (firewall, Docker port mapping), "
                          "then run Set up again.",
    BRIDGE_REMOVED: "Remote Bridge removed from the TV.",
    BRIDGE_UPDATE_PROMPT: "The Remote Bridge app on the TV is version %s; this Kodi carries %s. Update it now?",
    BRIDGE_UPDATING: "Updating Remote Bridge on the TV...",
    BRIDGE_UPDATED: "Remote Bridge updated to %s.",
    BRIDGE_UPDATE_TIMEOUT: "The Remote Bridge update didn't finish. Check Homebrew Channel on the TV, then run Set up again.",
    BRIDGE_UPDATE_REPO: "This version of the TV app can't update itself. Kodi will open Homebrew Channel on the TV with this Kodi's repository: open Remote Bridge for Kodi there and update it. Kodi brings you back here when it's done.",
    BRIDGE_UPDATE_AVAILABLE: "Update available",
    TV_NOT_CONNECTED: "The TV isn't connected.",
    32500: "OLED pixel brightness",
    32501: "HDR dynamic tone mapping",
    32502: "Noise reduction",
    32503: "MPEG noise reduction",
    32504: "Smooth gradation",
    32505: "Super resolution",
    32506: "Eye comfort mode",
    32510: "Off",
    32511: "On",
    32512: "Low",
    32513: "Medium",
    32514: "High",
    32515: "Auto",
    32516: "HGIG",
    32411: "Off",
    32412: "Cinematic Movement",
    32413: "Natural",
    32414: "Smooth",
}


def get(string_id: int) -> str:
    try:
        text = xbmcaddon.Addon(ADDON_ID).getLocalizedString(string_id)
    except Exception:  # noqa: BLE001
        text = ""
    if not text or text.startswith("#"):
        return _FALLBACK.get(string_id, str(string_id))
    return text
