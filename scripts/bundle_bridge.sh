#!/bin/bash
# Bundle the Remote Bridge TV app into the add-on (resources/bridge/), which
# serves it to the TV's Homebrew Channel as a one-package repository.
#
#   scripts/bundle_bridge.sh [path to kodi-remote-bridge]   (default: ../kodi-remote-bridge)
#
# Builds the ipk there (tools/pack_ipk.py), then copies it, its icon, and
# bridge.json (what resources/lib/bridgerepo.py needs to describe it).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$(cd "${1:-$ROOT/../kodi-remote-bridge}" && pwd)"
OUT="$ROOT/resources/bridge"

"$SRC/build.sh" >/dev/null
IPK="$(ls "$SRC"/build/*.ipk)"
rm -rf "$OUT" && mkdir -p "$OUT"
cp "$IPK" "$OUT/"
cp "$SRC/src/largeIcon.png" "$OUT/icon.png"
python3 - "$SRC/src/appinfo.json" "$OUT/$(basename "$IPK")" "$OUT/bridge.json" <<'PY'
import hashlib, json, os, sys
appinfo, ipk, out = sys.argv[1:]
info = json.load(open(appinfo))
data = open(ipk, "rb").read()
json.dump({
    "id": info["id"], "version": info["version"], "title": info["title"],
    "appDescription": info["appDescription"], "ipk": os.path.basename(ipk),
    "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
}, open(out, "w"), indent=2)
PY
echo "bundled $(basename "$IPK")"
