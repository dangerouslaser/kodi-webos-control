"""Typed view of the addon settings."""

from __future__ import annotations

from dataclasses import dataclass

import xbmcaddon

ADDON_ID = "service.webostv"
# Spinner settings store an index into these.
TV_OFF_ACTIONS = ("none", "pause", "stop", "shutdown")
SWITCH_AWAY_ACTIONS = ("none", "pause", "stop")
# TruMotion values; "" = leave unchanged.
MOTION_MODES = ("", "off", "cinemaClear", "natural", "smooth")
# Sound outputs a rule can pick; "" = leave unchanged.
# Source-quality cleanup level; "" = leave unchanged.
QUALITY_LEVELS = ("", "off", "low", "medium", "high")
# Remote Bridge: how far the remote must move to bring the pointer back after
# a button press (setting index -> distance in the TV's 1920-wide coordinates).
POINTER_WAKES = (900, 300, 100, 25)
# Remote Bridge scroll wheel: items per notch (setting index -> speed).
WHEEL_SPEEDS = (1 / 3, 0.5, 1.0, 2.0)
# The TV's own pointer settings (webOS key -> add-on setting, its values by
# setting index; None: a toggle, "on"/"off"). The TV is the source of truth:
# the Remote Bridge app reports them, and changes here are sent to the TV.
TV_POINTER: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "pointerSpeed": ("tv_pointer_speed", ("slow", "normal", "fast")),
    "pointerSize": ("tv_pointer_size", ("small", "medium", "large")),
    "pointerShape": ("tv_pointer_shape", ("auto", "a", "b", "c", "d")),
    "pointerAlignment": ("tv_pointer_align", None),
    "cursorAutoRemover": ("tv_pointer_autohide", None),
}
SOUND_RULE_OUTPUTS = ("", "tv_speaker", "external_arc", "external_optical", "bt_soundbar", "headphone")


def _option(addon: xbmcaddon.Addon, key: str, options: tuple[str, ...]) -> str:
    try:
        index = int(addon.getSetting(key) or 0)
    except ValueError:
        index = 0
    return options[index] if 0 <= index < len(options) else options[0]


def _wake(addon: xbmcaddon.Addon) -> int:
    try:
        index = int(addon.getSetting("bridge_pointer_wake") or 1)
    except ValueError:
        index = 1
    return POINTER_WAKES[index] if 0 <= index < len(POINTER_WAKES) else POINTER_WAKES[1]


def _wheel_speed(addon: xbmcaddon.Addon) -> float:
    try:
        index = int(addon.getSetting("bridge_wheel_speed") or 2)
    except ValueError:
        index = 2
    return WHEEL_SPEEDS[index] if 0 <= index < len(WHEEL_SPEEDS) else 1.0


def _tv_pointer(addon: xbmcaddon.Addon) -> tuple[tuple[str, str], ...]:
    values = []
    for key, (setting, options) in TV_POINTER.items():
        if options is None:
            values.append((key, "on" if addon.getSettingBool(setting) else "off"))
        else:
            values.append((key, _option(addon, setting, options)))
    return tuple(values)


def tv_pointer_to_addon(tv: dict[str, str]) -> dict[str, str]:
    """The TV's reported pointer settings as add-on setting values (strings)."""
    out = {}
    for key, value in tv.items():
        if key not in TV_POINTER:
            continue
        setting, options = TV_POINTER[key]
        if options is None:
            out[setting] = "true" if value == "on" else "false"
        elif value in options:
            out[setting] = str(options.index(value))
    return out


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    loopback_fallback: bool = True
    auto_discover: bool = True
    mac: str = ""
    connect_timeout: float = 10.0
    toast_on_connect: bool = False
    backoff_cap: float = 60.0
    debug: bool = False
    # Phase 2: playback rules
    rules_enabled: bool = False
    preset_sdr: str = ""
    preset_hdr10: str = ""
    preset_hlg: str = ""
    preset_dv: str = ""
    restore_on_stop: bool = True
    rules_debounce: float = 1.5
    # TV integration
    kodi_input: str = ""
    on_tv_off: str = "none"
    on_switch_away: str = "none"
    resume_on_return: bool = True
    transparent_control: bool = False
    # Remote Bridge: the TV remote drives Kodi (rooted TVs)
    remote_bridge: bool = False
    bridge_port: int = 9779
    bridge_token: str = ""
    bridge_pointer: bool = False
    bridge_pointer_wake: int = 300
    bridge_pointer_return: bool = True
    bridge_wheel_speed: float = 1.0
    # The TV's pointer settings as (webOS key, value) pairs (see TV_POINTER).
    tv_pointer: tuple[tuple[str, str], ...] = ()
    # Motion smoothing by frame rate (blind writes: TVs won't report it)
    motion_film: str = ""
    motion_video: str = ""
    motion_restore: str = ""
    # Source quality: noise reduction etc. by source
    quality_livetv: str = ""
    quality_sd: str = ""
    quality_hd: str = ""
    # Sound output by content
    sound_video: str = ""
    sound_livetv: str = ""
    sound_music: str = ""

    @classmethod
    def load(cls, addon: xbmcaddon.Addon | None = None) -> Settings:
        addon = addon or xbmcaddon.Addon(ADDON_ID)

        def number(key: str, default: float) -> float:
            try:
                value = float(addon.getSetting(key))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        return cls(
            host=(addon.getSetting("host") or "127.0.0.1").strip(),
            loopback_fallback=addon.getSettingBool("loopback_fallback"),
            auto_discover=addon.getSettingBool("auto_discover"),
            mac=addon.getSetting("mac").strip(),
            connect_timeout=number("connect_timeout", 10.0),
            toast_on_connect=addon.getSettingBool("toast_on_connect"),
            backoff_cap=number("backoff_cap", 60.0),
            debug=addon.getSettingBool("debug"),
            rules_enabled=addon.getSettingBool("rules_enabled"),
            preset_sdr=addon.getSetting("preset_sdr").strip(),
            preset_hdr10=addon.getSetting("preset_hdr10").strip(),
            preset_hlg=addon.getSetting("preset_hlg").strip(),
            preset_dv=addon.getSetting("preset_dv").strip(),
            restore_on_stop=addon.getSettingBool("restore_on_stop"),
            rules_debounce=number("rules_debounce", 1.5),
            kodi_input=addon.getSetting("kodi_input").strip(),
            on_tv_off=_option(addon, "on_tv_off", TV_OFF_ACTIONS),
            on_switch_away=_option(addon, "on_switch_away", SWITCH_AWAY_ACTIONS),
            resume_on_return=addon.getSettingBool("resume_on_return"),
            transparent_control=addon.getSettingBool("transparent_control"),
            remote_bridge=addon.getSettingBool("remote_bridge"),
            bridge_port=int(number("bridge_port", 9779)),
            bridge_token=addon.getSetting("bridge_token").strip(),
            bridge_pointer=addon.getSettingBool("bridge_pointer"),
            bridge_pointer_wake=_wake(addon),
            bridge_pointer_return=addon.getSettingBool("bridge_pointer_return"),
            bridge_wheel_speed=_wheel_speed(addon),
            tv_pointer=_tv_pointer(addon),
            motion_film=_option(addon, "motion_film", MOTION_MODES),
            motion_video=_option(addon, "motion_video", MOTION_MODES),
            motion_restore=_option(addon, "motion_restore", MOTION_MODES),
            quality_livetv=_option(addon, "quality_livetv", QUALITY_LEVELS),
            quality_sd=_option(addon, "quality_sd", QUALITY_LEVELS),
            quality_hd=_option(addon, "quality_hd", QUALITY_LEVELS),
            sound_video=_option(addon, "sound_video", SOUND_RULE_OUTPUTS),
            sound_livetv=_option(addon, "sound_livetv", SOUND_RULE_OUTPUTS),
            sound_music=_option(addon, "sound_music", SOUND_RULE_OUTPUTS),
        )

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")
