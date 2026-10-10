"""Typed view of the addon settings."""

from __future__ import annotations

from dataclasses import dataclass

import xbmcaddon

ADDON_ID = "service.webostv"


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
        )

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")
