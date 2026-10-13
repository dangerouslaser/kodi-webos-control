"""Picture settings Kodi can change besides the preset (OSD quick picks,
source-quality rules).

Values verified on webOS 10 (G5) by writing them and reading them back.
webOS keeps most of these per input and picture mode; Eye Comfort is
TV-wide. Older webOS (5.x) accepts the writes but won't report values.
"""

from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("off", "low", "medium", "high")


@dataclass(frozen=True)
class Adjustment:
    key: str
    label: int  # string id
    values: tuple[str, ...]
    numeric: bool = False

    def parse(self, value: str) -> str | None:
        """Normalize a value; None if this setting doesn't take it."""
        value = str(value).strip()
        if self.numeric:
            return str(int(value)) if value.isdigit() and 0 <= int(value) <= 100 else None
        for allowed in self.values:
            if value.casefold() == allowed.casefold():
                return allowed
        return None

    def wire(self, value: str) -> str | int:
        return int(value) if self.numeric else value


ADJUSTMENTS = {
    a.key: a
    for a in (
        Adjustment("backlight", 32500, ("100", "90", "80", "70", "60", "50", "40", "30", "20"), numeric=True),
        Adjustment("hdrDynamicToneMapping", 32501, ("on", "off", "HGIG")),
        Adjustment("noiseReduction", 32502, (*LEVELS, "auto")),
        Adjustment("mpegNoiseReduction", 32503, (*LEVELS, "auto")),
        Adjustment("smoothGradation", 32504, LEVELS),
        Adjustment("superResolution", 32505, LEVELS),
        Adjustment("eyeComfortMode", 32506, ("off", "on")),
    )
}

VALUE_LABELS = {"off": 32510, "on": 32511, "low": 32512, "medium": 32513, "high": 32514,
                "auto": 32515, "HGIG": 32516}

# Source-quality rules set all of these to one level.
QUALITY_KEYS = ("noiseReduction", "mpegNoiseReduction", "smoothGradation", "superResolution")
# Setting index -> level; "" = leave unchanged.
QUALITY_LEVELS = ("", *LEVELS)
# Below this video height counts as SD; from UHD_MIN_HEIGHT up, 4K (and 1440p).
# Scope films are wide, not tall: 3840x1600 is 4K, 1920x1080 is HD.
HD_MIN_HEIGHT = 720
UHD_MIN_HEIGHT = 1200


def source_class(content: str, height: int) -> str | None:
    """"livetv", "sd", "hd" or "uhd" for what's playing; None when unknown."""
    if content == "livetv":
        return "livetv"
    if height <= 0:
        return None
    if height < HD_MIN_HEIGHT:
        return "sd"
    return "hd" if height < UHD_MIN_HEIGHT else "uhd"
