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
