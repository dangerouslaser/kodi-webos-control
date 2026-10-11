"""plugin://service.webostv/ — the TV's apps and inputs as list items.

    plugin://service.webostv/?view=apps      apps in the TV launcher's order
    plugin://service.webostv/?view=inputs    external inputs
    ...?action=app&id=<app id>               launch an app (item click)
    ...?action=input&id=<input id>           switch input (item click)

Skins use the views as widgets. Listings come from addon_data/launcher.json,
which the service keeps current (resources/lib/launcher.py).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

import xbmcgui
import xbmcplugin

from . import ipc, launcher


def main(argv: list[str]) -> None:
    base = argv[0]
    handle = int(argv[1]) if len(argv) > 1 and argv[1].lstrip("-").isdigit() else -1
    query = dict(parse_qsl(argv[2].lstrip("?"))) if len(argv) > 2 else {}
    action = query.get("action")
    if action in ("app", "input") and query.get("id"):
        ipc.send(action, [query["id"]])
        return
    view = "inputs" if query.get("view") == "inputs" else "apps"
    kind = "input" if view == "inputs" else "app"
    listing = []
    for item in launcher.read().get(view) or []:
        li = xbmcgui.ListItem(item.get("title") or item.get("id"), offscreen=True)
        if item.get("icon"):
            li.setArt({"icon": item["icon"], "thumb": item["icon"], "poster": item["icon"]})
        li.setProperty("WebOS.Id", str(item.get("id", "")))
        if item.get("color"):
            li.setProperty("WebOS.Color", str(item["color"]))
        url = f"{base}?{urlencode({'action': kind, 'id': item.get('id', '')})}"
        listing.append((url, li, False))
    if handle >= 0:
        xbmcplugin.addDirectoryItems(handle, listing, len(listing))
        xbmcplugin.setContent(handle, "files")
        xbmcplugin.endOfDirectory(handle, succeeded=True, cacheToDisc=False)
