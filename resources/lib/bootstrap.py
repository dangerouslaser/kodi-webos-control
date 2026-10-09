"""Import plumbing for the vendored protocol stack.

Call ``install()`` before importing ``aiowebostv``. It puts the vendor
directory first on ``sys.path`` (so the vendored pure-Python ``websockets``
wins over any system copy) and registers ``aiohttp_shim`` as ``aiohttp``.
aiowebostv source is never edited; upgrading it is a straight file swap.
"""

from __future__ import annotations

import os
import sys

MIN_PYTHON = (3, 11)

LIB_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(LIB_DIR, "vendor")


def python_supported() -> bool:
    """Return True if the running interpreter can host aiowebostv."""
    return sys.version_info >= MIN_PYTHON


def install() -> None:
    """Make vendored packages importable and alias aiohttp to the shim."""
    if VENDOR_DIR not in sys.path:
        sys.path.insert(0, VENDOR_DIR)
    if LIB_DIR not in sys.path:
        sys.path.insert(1, LIB_DIR)

    existing = sys.modules.get("aiohttp")
    if existing is not None and getattr(existing, "__name__", "") == "aiohttp_shim":
        return

    import aiohttp_shim  # noqa: PLC0415

    sys.modules["aiohttp"] = aiohttp_shim
