"""Build an installable Kodi addon zip: dist/service.webostv-<version>.zip."""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADDON_ID = "service.webostv"
INCLUDE = ["addon.xml", "LICENSE.txt", "README.md", "service.py", "default.py", "context.py", "resources"]
EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".so", ".pyd", ".c"}


def version() -> str:
    match = re.search(r'<addon[^>]*\sversion="([^"]+)"', (ROOT / "addon.xml").read_text())
    if not match:
        sys.exit("version not found in addon.xml")
    return match.group(1)


def files():
    for name in INCLUDE:
        path = ROOT / name
        if path.is_file():
            yield path
            continue
        for p in sorted(path.rglob("*")):
            if p.is_file() and not EXCLUDE_DIRS & set(p.parts) and p.suffix not in EXCLUDE_SUFFIXES:
                yield p


def main() -> None:
    out = ROOT / "dist" / f"{ADDON_ID}-{version()}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files():
            zf.write(p, f"{ADDON_ID}/{p.relative_to(ROOT).as_posix()}")
    print(out)


if __name__ == "__main__":
    main()
