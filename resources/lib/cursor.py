"""The TV's own pointer images, laid out for the skin's Pointer.xml.

The Remote Bridge app reads the webOS cursor images from the TV's firmware
and sends them over, so Kodi's cursor matches that TV without anyone shipping
LG's artwork. Pointer.xml has fixed geometry, so every image is copied onto a
CANVAS-sized transparent canvas with its hotspot (the fingertip, the drop's
tip) at HOTSPOT. The skin's bundled pointers use the same layout.
"""

from __future__ import annotations

import hashlib
import os

from . import pngutil

CANVAS = (128, 128)
HOTSPOT = (40, 12)
KINDS = ("point", "hover", "hold", "drag")


def install(images: dict[str, bytes], directory: str) -> dict[str, str]:
    """Write laid-out pointers into `directory`; kind -> file path.

    File names carry a content hash: Kodi caches textures by path. Images
    that can't be read are skipped (the skin falls back to its own).
    """
    os.makedirs(directory, exist_ok=True)
    paths: dict[str, str] = {}
    for kind in KINDS:
        data = images.get(kind)
        if not data:
            continue
        try:
            width, height, pixels = pngutil.read_rgba(data)
        except (pngutil.PngError, ValueError, OSError):
            continue
        hotspot = pngutil.tip(width, height, pixels)
        placed = pngutil.place(width, height, pixels, hotspot, CANVAS, HOTSPOT)
        digest = hashlib.sha1(data).hexdigest()[:12]
        path = os.path.join(directory, f"{kind}-{digest}.png")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(pngutil.write_rgba(*CANVAS, placed))
        paths[kind] = path
    keep = {os.path.basename(p) for p in paths.values()}
    for name in os.listdir(directory):
        if name.endswith(".png") and name not in keep:
            os.remove(os.path.join(directory, name))
    return paths
