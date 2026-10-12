"""The TV's own pointer images, laid out for the skin's Pointer.xml.

The Remote Bridge app reads the webOS cursor images from the TV's firmware
and sends them over, so Kodi's cursor matches that TV without anyone shipping
LG's artwork. Pointer.xml has fixed geometry, so every image is copied onto a
CANVAS-sized transparent canvas with its hotspot (the fingertip, the drop's
tip) at HOTSPOT. The skin's bundled pointers use the same layout.
Pointer.xml reads them from special://profile/addon_data/service.webostv/pointer/.
"""

from __future__ import annotations

import os

from . import pngutil

CANVAS = (128, 128)
HOTSPOT = (40, 12)
KINDS = ("point", "hover", "hold", "drag")


HOTSPOT_RULES = {"tip": pngutil.tip, "corner": pngutil.corner}


def install(images: dict[str, bytes], directory: str,
            hotspot: str = "tip") -> tuple[dict[str, str], bool]:
    """Write laid-out pointers as <kind>.png in `directory`.

    Returns (kind -> path, whether any file changed). The skin's Pointer.xml
    loads these fixed paths when the skin loads, so a change only shows after
    a skin reload. Images that can't be read are skipped (the skin falls back
    to its own pointer for those). `hotspot` says where each image's click
    point is: "tip" (topmost point: the classic hand) or "corner" (the
    current webOS pointer's pointed top-left corner).
    """
    find = HOTSPOT_RULES.get(hotspot, pngutil.tip)
    os.makedirs(directory, exist_ok=True)
    paths: dict[str, str] = {}
    changed = False
    for kind in KINDS:
        path = os.path.join(directory, f"{kind}.png")
        data = images.get(kind)
        png = _laid_out(data, find) if data else None
        if png is None:
            if os.path.exists(path):
                os.remove(path)
                changed = True
            continue
        try:
            with open(path, "rb") as f:
                same = f.read() == png
        except OSError:
            same = False
        if not same:
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(png)
            os.replace(tmp, path)
            changed = True
        paths[kind] = path
    for name in os.listdir(directory):
        if name.endswith(".png") and name[:-4] not in paths:
            os.remove(os.path.join(directory, name))  # older hash-named files
    return paths, changed


def _laid_out(data: bytes, find) -> bytes | None:
    try:
        width, height, pixels = pngutil.read_rgba(data)
    except (pngutil.PngError, ValueError, OSError):
        return None
    hotspot = find(width, height, pixels)
    return pngutil.write_rgba(*CANVAS, pngutil.place(width, height, pixels, hotspot, CANVAS, HOTSPOT))
