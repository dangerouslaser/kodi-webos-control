"""Video context menu: play with a one-off TV setup.

Each item arms a one-shot override in the service (``next_playback``),
then starts playback the way Kodi would. The service undoes the override
when playback stops.
"""

from __future__ import annotations

import json
from typing import Any

import xbmc
import xbmcgui

from . import ipc, lang, presets
from . import kodilog as log
from .automations import normalize_hdr_type

ADDON_NAME = "LG webOS TV"

OVERRIDES: dict[str, dict[str, Any]] = {
    "filmmaker": {"picture": "filmmaker"},
    "bright": {"picture": "bright"},
    "screen_off": {"screen_off": True},
    "sleep": {"power_off_at_end": True},
}
_DB_KEYS = {"movie": "movieid", "episode": "episodeid", "musicvideo": "musicvideoid"}


def main(argv: list[str], listitem: Any) -> None:
    choice = argv[1].strip().lower() if len(argv) > 1 else ""
    if choice == "preset":
        override = choose_preset()
    else:
        override = OVERRIDES.get(choice)
    if not override:
        return
    reply = ipc.call("next_playback", [override])
    if not reply.get("ok"):
        message = reply.get("message") or lang.get(lang.ACTION_FAILED)
        if reply.get("error") == "timeout":
            message = lang.get(lang.NOT_RUNNING)
        xbmcgui.Dialog().notification(ADDON_NAME, message, xbmcgui.NOTIFICATION_ERROR)
        return
    play(listitem)


def item_kind() -> str | None:
    """HDR type of the focused item, if Kodi has scanned its streams."""
    hdr = xbmc.getInfoLabel("ListItem.HdrType")
    if not hdr and not xbmc.getInfoLabel("ListItem.VideoResolution"):
        return None  # no stream details: unknown, not SDR
    return normalize_hdr_type(hdr)


def choose_preset() -> dict[str, Any] | None:
    reply = ipc.call("picture_modes")
    if not reply.get("ok"):
        xbmcgui.Dialog().notification(
            ADDON_NAME, reply.get("message") or lang.get(lang.NOT_RUNNING),
            xbmcgui.NOTIFICATION_ERROR,
        )
        return None
    values = [m["value"] for m in reply["result"].get("modes", [])]
    kind = item_kind()
    if kind is not None:
        values = presets.for_kind(kind, values)
    if not values:
        return None
    index = xbmcgui.Dialog().select(lang.get(lang.PICTURE_PRESET), [presets.label(v) for v in values])
    if index < 0:
        return None
    return {"picture": values[index]}


def _format_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def play(listitem: Any) -> None:
    """Start the item through Player.Open, asking to resume like Kodi does."""
    dbid, mediatype, resume_at = 0, "", 0.0
    if listitem is not None:
        try:
            tag = listitem.getVideoInfoTag()
            dbid, mediatype = tag.getDbId(), tag.getMediaType()
            resume_at = tag.getResumeTime()
        except Exception:  # noqa: BLE001
            pass
    path = (listitem.getPath() if listitem is not None else "") or xbmc.getInfoLabel(
        "ListItem.FileNameAndPath"
    )
    key = _DB_KEYS.get(mediatype)
    item: dict[str, Any] = {key: dbid} if key and dbid and dbid > 0 else {"file": path}

    resume = False
    if resume_at and resume_at > 0:
        options = [lang.get(lang.RESUME_FROM) % _format_time(resume_at), lang.get(lang.FROM_START)]
        index = xbmcgui.Dialog().contextmenu(options)
        if index < 0:
            return
        resume = index == 0
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "Player.Open",
        "params": {"item": item, "options": {"resume": resume}},
    }
    log.debug(f"context play {item} resume={resume}")
    xbmc.executeJSONRPC(json.dumps(request))
