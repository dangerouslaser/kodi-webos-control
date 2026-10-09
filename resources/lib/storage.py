"""JSON files under special://profile/addon_data/service.webostv/."""

from __future__ import annotations

import json
import os
from typing import Any

import xbmcvfs

PROFILE_DIR = "special://profile/addon_data/service.webostv/"


def data_path(name: str) -> str:
    base = xbmcvfs.translatePath(PROFILE_DIR)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, name)


def read_json(name: str, default: Any = None) -> Any:
    try:
        with open(data_path(name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(name: str, value: Any) -> None:
    """Write atomically so a crash never leaves a truncated file."""
    path = data_path(name)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
