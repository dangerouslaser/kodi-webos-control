"""Fake xbmcvfs module: special://profile maps to a per-test temp dir."""

from __future__ import annotations

import os
from pathlib import Path

PROFILE_ROOT = Path("/tmp/fake-kodi-profile")


def translatePath(path: str) -> str:  # noqa: N802
    if path.startswith("special://profile/"):
        return str(PROFILE_ROOT / path[len("special://profile/"):])
    return path


def mkdirs(path: str) -> bool:
    os.makedirs(path, exist_ok=True)
    return True


def exists(path: str) -> bool:
    return os.path.exists(path)
