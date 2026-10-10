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
}


def get(string_id: int) -> str:
    try:
        text = xbmcaddon.Addon(ADDON_ID).getLocalizedString(string_id)
    except Exception:  # noqa: BLE001
        text = ""
    if not text or text.startswith("#"):
        return _FALLBACK.get(string_id, str(string_id))
    return text
