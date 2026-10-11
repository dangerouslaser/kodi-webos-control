"""Keep addon_data/launcher.json: the TV's apps and inputs with cached icons.

The service refreshes it when the TV's app or input list changes; the
plugin (plugin://service.webostv/) only reads it, so skin widgets load
instantly and still show while the TV is off. The TV serves icons over
HTTPS with a self-signed certificate, so they're downloaded here, only
from the TV's own address, into addon_data/icons/.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import ssl
import urllib.request
from typing import Any
from urllib.parse import urlparse

import xbmcvfs

from . import kodilog as log
from . import storage

FILE = "launcher.json"
ICON_DIR = "special://profile/addon_data/service.webostv/icons/"
ICON_TIMEOUT = 4.0


def read() -> dict[str, Any]:
    data = storage.read_json(FILE, {}) or {}
    return data if isinstance(data, dict) else {}


def icon_path(url: str) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        ext = ".png"
    folder = xbmcvfs.translatePath(ICON_DIR)
    return os.path.join(folder, hashlib.sha1(url.encode()).hexdigest() + ext)


def download(url: str, host: str) -> str:
    """Local path of a cached icon, downloading it if needed; "" if not."""
    if not url:
        return ""
    path = icon_path(url)
    if os.path.exists(path):
        return path
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not host or parsed.hostname != host:
        return ""  # only fetch from the TV itself
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # the TV's certificate is self-signed
    try:
        with urllib.request.urlopen(url, timeout=ICON_TIMEOUT, context=context) as resp:  # noqa: S310
            data = resp.read(2_000_000)
    except Exception as err:  # noqa: BLE001
        log.debug(f"icon {url}: {err!r}")
        return ""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


async def refresh(points: dict[str, Any]) -> None:
    """Cache icons for TvController.launch_points() and write the file."""
    host = str(points.get("host") or "")
    loop = asyncio.get_running_loop()
    for key in ("apps", "inputs"):
        items = points.get(key) or []
        paths = await asyncio.gather(
            *(loop.run_in_executor(None, download, str(i.get("icon") or ""), host) for i in items)
        )
        for item, path in zip(items, paths, strict=True):
            item["icon"] = path
    storage.write_json(FILE, {"apps": points.get("apps") or [], "inputs": points.get("inputs") or []})
    log.debug(f"launcher: {len(points.get('apps') or [])} apps, {len(points.get('inputs') or [])} inputs")
