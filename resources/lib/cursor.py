"""The TV's own pointer images, laid out for the skin's Pointer.xml.

The Remote Bridge app reads the webOS cursor images from the TV's firmware
and sends them over, so Kodi's cursor matches that TV without anyone shipping
LG's artwork. Pointer.xml has fixed geometry, so every image is copied onto a
CANVAS-sized transparent canvas with its hotspot (the pointed corner, the
fingertip) at HOTSPOT, its middle: the pointer may point any way, and come
in three sizes. The skin's bundled pointers use the same layout.
Pointer.xml reads them from special://profile/addon_data/service.webostv/pointer/.

The current webOS pointer is also animated, and the bridge sends those frames
too (FRAME_SETS): it appears (enter0..5: growing from a dot) and disappears
(exit0..2), shrinks when pressed (press0..5), and turns into a wheel while the
wheel scrolls (scroll0..21: a pill, 0-5, whose dot runs from the middle to the
bottom, 6-12, then from the bottom to the top, 13-21). They become
anim_<look>.apng (ANIMATIONS). The skin plays one while WebOS.PointerLook (set
from the bridge) names it, and the press one for Kodi's click pointer once
WebOS.PointerAnimated is set (has_animations()).
"""

from __future__ import annotations

import functools
import os

from . import pngutil

CANVAS = (256, 256)
HOTSPOT = (128, 128)
KINDS = ("point", "hover", "hold", "drag")
# Each animation's frames, and the one placed like the still pointer (full size).
FRAME_SETS = {"scroll": (22, 0), "press": (6, 0), "enter": (6, 5), "exit": (3, 0)}
FRAME_MS = 33
# WebOS.PointerLook (and "press") -> the frames it plays; each holds on its
# last one. "downx"/"upx": the wheel turned the other way with the pill up.
ANIMATIONS = {
    "down": ("scroll", (*range(0, 13),)),
    "up": ("scroll", (*range(0, 8), *range(17, 22))),
    "downx": ("scroll", (*range(21, 12, -1),)),
    "upx": ("scroll", (*range(13, 22),)),
    "end": ("scroll", (*range(5, -1, -1),)),
    "enter": ("enter", (*range(6),)),
    "exit": ("exit", (*range(3),)),
    "press": ("press", (*range(6),)),
}


HOTSPOT_RULES = {
    "tip": pngutil.tip,
    "corner": pngutil.corner,
    "corner-tr": functools.partial(pngutil.corner, which="tr"),
    "corner-bl": functools.partial(pngutil.corner, which="bl"),
    "corner-br": functools.partial(pngutil.corner, which="br"),
}


def install(images: dict[str, bytes], directory: str,
            hotspot: str = "tip") -> tuple[dict[str, str], bool]:
    """Write laid-out pointers as <kind>.png in `directory`.

    Returns (kind -> path, whether any file changed). The skin's Pointer.xml
    loads these fixed paths when the skin loads, so a change only shows after
    a skin reload. Images that can't be read are skipped (the skin falls back
    to its own pointer for those). `hotspot` says where each image's click
    point is: "tip" (topmost point: the classic hand) or "corner" (the
    current webOS pointer's pointed top-left corner; "corner-tr", "corner-bl"
    and "corner-br" when it's set to point another way).
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
        paths[kind], wrote = _write(path, png)
        changed |= wrote
    for look, (path, wrote) in _animations(images, find, directory).items():
        paths[f"anim_{look}"] = path
        changed |= wrote
    for name in os.listdir(directory):
        stem, ext = os.path.splitext(name)
        if ext in (".png", ".apng") and stem not in paths:
            os.remove(os.path.join(directory, name))  # older hash-named files, a pointer gone
            changed = True
    return paths, changed


def has_animations(directory: str) -> bool:
    """Whether install() wrote every animation (the TV has them)."""
    return all(os.path.exists(os.path.join(directory, f"anim_{look}.apng")) for look in ANIMATIONS)


def _animations(images: dict[str, bytes], find, directory: str) -> dict[str, tuple[str, bool]]:
    """look -> (path, changed) for each anim_<look>.apng, if the TV sent every
    frame of them all. A set's frames go where its full-size frame puts the
    hotspot, so the pill grows out of the pointer, the dot into it."""
    placed = {}
    for name, (count, full) in FRAME_SETS.items():
        frames = []
        for i in range(count):
            try:
                frames.append(pngutil.read_rgba(images[f"{name}{i}"]))
            except (KeyError, pngutil.PngError, ValueError, OSError):
                return {}
        hotspot = find(*frames[full])
        placed[name] = [pngutil.place(w, h, px, hotspot, CANVAS, HOTSPOT) for w, h, px in frames]
    out = {}
    for look, (name, sequence) in ANIMATIONS.items():
        delays = [FRAME_MS] * (len(sequence) - 1) + [65535]
        data = pngutil.write_apng(*CANVAS, [placed[name][i] for i in sequence], delays)
        out[look] = _write(os.path.join(directory, f"anim_{look}.apng"), data)
    return out


def _write(path: str, data: bytes) -> tuple[str, bool]:
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return path, False
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path, True


def _laid_out(data: bytes, find) -> bytes | None:
    try:
        width, height, pixels = pngutil.read_rgba(data)
    except (pngutil.PngError, ValueError, OSError):
        return None
    hotspot = find(width, height, pixels)
    return pngutil.write_rgba(*CANVAS, pngutil.place(width, height, pixels, hotspot, CANVAS, HOTSPOT))
