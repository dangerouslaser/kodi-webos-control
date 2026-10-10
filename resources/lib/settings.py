"""Typed view of the addon settings."""

from __future__ import annotations

from dataclasses import dataclass

import xbmcaddon

ADDON_ID = "service.webostv"
# Spinner settings store an index into these.
TV_OFF_ACTIONS = ("none", "pause", "stop", "shutdown")
SWITCH_AWAY_ACTIONS = ("none", "pause", "stop")


def _option(addon: xbmcaddon.Addon, key: str, options: tuple[str, ...]) -> str:
    try:
        index = int(addon.getSetting(key) or 0)
    except ValueError:
        index = 0
    return options[index] if 0 <= index < len(options) else options[0]


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
        )

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")
