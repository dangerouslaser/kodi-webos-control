"""Fake xbmcaddon module."""

from __future__ import annotations

settings: dict[str, str] = {}
_DEFAULTS = {
    "host": "127.0.0.1",
    "loopback_fallback": "true",
    "auto_discover": "true",
    "mac": "",
    "connect_timeout": "10",
    "toast_on_connect": "false",
    "backoff_cap": "60",
    "debug": "false",
    "rules_enabled": "false",
    "preset_sdr": "",
    "preset_hdr10": "",
    "preset_hlg": "",
    "preset_dv": "",
    "restore_on_stop": "true",
    "rules_debounce": "1.5",
    "kodi_input": "",
    "on_tv_off": "0",
    "on_switch_away": "0",
    "resume_on_return": "true",
    "transparent_control": "false",
}


def _reset() -> None:
    settings.clear()
    settings.update(_DEFAULTS)


_reset()


class Addon:
    def __init__(self, id: str | None = None) -> None:  # noqa: A002
        self._id = id or "service.webostv"

    def getAddonInfo(self, key: str) -> str:  # noqa: N802
        return {"id": self._id, "name": "LG webOS TV Control", "version": "0.1.0",
                "path": ".", "profile": "special://profile/addon_data/service.webostv/"}.get(key, "")

    def getSetting(self, key: str) -> str:  # noqa: N802
        return settings.get(key, "")

    def getSettingBool(self, key: str) -> bool:  # noqa: N802
        return settings.get(key, "false").lower() == "true"

    def getSettingInt(self, key: str) -> int:  # noqa: N802
        return int(settings.get(key) or 0)

    def getSettingNumber(self, key: str) -> float:  # noqa: N802
        return float(settings.get(key) or 0)

    def setSetting(self, key: str, value: str) -> None:  # noqa: N802
        settings[key] = value

    def getLocalizedString(self, string_id: int) -> str:  # noqa: N802
        return f"#{string_id}"
