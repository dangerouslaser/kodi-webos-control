"""LG remote buttons, and which of them Kodi takes while the Remote Bridge has the remote.

Every LG remote, Magic Remote or IR, reaches the TV's input devices with the
same Linux key codes: LG's input service (lginput2) translates them. So one
table serves every model. Models differ in which buttons they have: older
Magic Remotes send Home Hub as 1123 and newer ones 1023, IR remotes have their
own Options (799) and Search (217) codes, the MR25GA has no number keys. The
codes are as read on the TVs themselves by the homebrew remappers lginputhook,
magic_mapper and lginputmapper (verified on an MR25GA), and by this add-on on
a CX (MR20) and a G5 (MR25GA).

Kodi takes the buttons that drive it (navigation, OK, Back, playback, numbers,
colours, Info, Guide, Options) and leaves the TV's own buttons to the TV, even
while Kodi is on screen: Settings opens the TV's settings, Inputs its input
list, and so on. The bridge reports the paired Magic Remote (Remote): the
settings page shows it, and PROFILES holds what a model does differently. (Its
buttons don't narrow what Kodi takes: an IR remote, with its own number keys,
may be in use alongside a Magic Remote that has none.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Kodi's home screen; held, the TV's (see bridge.LONG_PRESS_BUTTONS).
KODI_HOME = "kodihome"

# Every known LG remote button: code -> name (for logs and the settings page).
LG_BUTTONS: dict[int, str] = {
    2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
    28: "OK", 103: "Up", 108: "Down", 105: "Left", 106: "Right",
    412: "Back", 174: "Exit", 158: "Back",
    113: "Mute", 114: "Volume down", 115: "Volume up", 402: "Channel up", 403: "Channel down",
    398: "Red", 399: "Green", 400: "Yellow", 401: "Blue",
    773: "Home", 102: "Home", 172: "Home", 1023: "Home Hub", 1123: "Home Hub",
    1124: "Accessibility", 139: "Settings", 241: "Inputs", 428: "Voice", 358: "Info",
    362: "Guide", 365: "Guide", 994: "Options", 799: "Options", 771: "Channels", 787: "List",
    1083: "Search", 217: "Search", 829: "SAP", 1116: "TV", 116: "Power",
    164: "Play/Pause", 207: "Play", 119: "Pause", 128: "Stop", 167: "Record",
    168: "Rewind", 208: "Fast forward", 163: "Next", 165: "Previous",
    370: "Subtitles", 392: "Audio",
    1037: "Netflix", 1038: "Prime Video", 1042: "Disney+", 1043: "LG Channels",
    1044: "Rakuten TV", 1086: "Alexa", 1107: "Sling", 1111: "Stan", 1117: "Google Assistant",
    1039: "ivi", 1041: "Hotstar", 1045: "Globoplay", 1047: "Okko", 1088: "Kinopoisk", 1089: "Watcha",
    1090: "U-NEXT", 1091: "FPT Play", 1092: "Shahid", 1095: "Hulu", 1096: "NHK+", 1097: "TOD",
    1099: "Freeview Play", 1102: "Sony LIV", 1108: "TVer", 1109: "Wavve", 1110: "Coupang Play",
    1120: "TV360", 1121: "VTVgo", 1125: "VK Video", 1126: "Premier",
}

# The remote's app buttons (each opens a streaming app; models carry a few,
# by region), in the order the settings list shows them. Any of them can be
# set to bring back Kodi instead (settings.kodi_buttons).
APP_BUTTONS: tuple[int, ...] = (
    1037, 1038, 1042, 1095, 1043, 1044, 1107, 1111, 1099, 1102, 1041, 1092, 1039, 1045, 1047, 1088,
    1089, 1090, 1091, 1096, 1097, 1108, 1109, 1110, 1120, 1121, 1125, 1126,
)

# Linux key code -> button of Kodi's remote keymap (remote.xml, "R1"). Kodi
# resolves them per window, like a real remote: OK selects in menus and opens
# the OSD in fullscreen video, and user keymaps apply. Codes not here are the
# TV's.
KODI_BUTTONS: dict[int, str] = {
    1: "back",            # KEY_ESC
    2: "one", 3: "two", 4: "three", 5: "four", 6: "five",
    7: "six", 8: "seven", 9: "eight", 10: "nine", 11: "zero",
    28: "select",         # OK
    103: "up", 105: "left", 106: "right", 108: "down",
    119: "pause",
    128: "stop",
    158: "back",          # KEY_BACK
    163: "skipplus",      # next
    164: "play",          # play/pause
    165: "skipminus",     # previous
    168: "reverse",       # rewind
    174: "back",          # Exit (IR remotes)
    207: "play",
    208: "forward",       # fast forward
    352: "select",        # KEY_OK
    358: "info",
    362: "guide",         # LG's Guide
    365: "guide",         # KEY_EPG (other remotes)
    370: "subtitle",
    392: "language",      # KEY_AUDIO
    398: "red", 399: "green", 400: "yellow", 401: "blue",
    402: "channelplus", 403: "channelminus",
    412: "back",          # the Magic Remote's Back
    994: "menu",          # Options (...): the context menu, the OSD in fullscreen video
    799: "menu",          # Options (IR remotes)
    773: KODI_HOME,       # the Magic Remote's Home
    102: KODI_HOME,       # KEY_HOME
    172: KODI_HOME,       # KEY_HOMEPAGE
}

# What a model does differently: model prefix -> {code: Kodi button, or None for
# the TV's}. Empty while every known model follows the shared table.
PROFILES: dict[str, dict[int, str | None]] = {}


@dataclass(frozen=True)
class Remote:
    """The paired Magic Remote, as the bridge reports it. None for what the
    TV doesn't say (webOS 5 gives only the model)."""

    model: str = ""
    paired: bool = False
    numeric: bool | None = None
    mute: bool | None = None
    cursor: bool | None = None

    @classmethod
    def from_report(cls, report: Any) -> Remote:
        if not isinstance(report, dict):
            return cls()

        def flag(key: str) -> bool | None:
            value = report.get(key)
            return value if isinstance(value, bool) else None

        return cls(model=str(report.get("model") or "")[:32], paired=bool(report.get("paired")),
                   numeric=flag("numeric"), mute=flag("mute"), cursor=flag("cursor"))

    @property
    def label(self) -> str:
        """"LG MR20", or "" when no Magic Remote is paired."""
        return f"LG {self.model}" if self.paired and self.model else ""


def kodi_buttons(remote: Remote | None = None) -> dict[int, str]:
    """The buttons Kodi takes from this remote (all of KODI_BUTTONS when it's unknown)."""
    buttons = dict(KODI_BUTTONS)
    if remote is None:
        return buttons
    for prefix, changes in PROFILES.items():
        if remote.model.startswith(prefix):
            for code, button in changes.items():
                if button is None:
                    buttons.pop(code, None)
                else:
                    buttons[code] = button
    return buttons


def button_name(code: int) -> str:
    return LG_BUTTONS.get(code, f"key {code}")
