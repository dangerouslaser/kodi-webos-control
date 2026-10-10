#!/bin/sh
# Dev deploy: copy the addon to the Kodi box over SSH and restart the
# service (disable + enable via JSON-RPC on the box).
#   scripts/deploy.sh            (KODI_HOST defaults to 192.168.1.175)
set -eu
HOST="${KODI_HOST:-192.168.1.175}"
ID=service.webostv
DEST=/storage/.kodi/addons/$ID
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

rpc() {
  printf '{"jsonrpc":"2.0","id":1,"method":"Addons.SetAddonEnabled","params":{"addonid":"%s","enabled":%s}}' "$ID" "$1" |
    nc -w 2 "$HOST" 9090 >/dev/null || true
}

rpc false
sleep 3
COPYFILE_DISABLE=1 tar -C "$ROOT" -cf - --exclude __pycache__ --exclude .DS_Store \
  addon.xml LICENSE.txt README.md service.py default.py context.py resources |
  ssh "root@$HOST" "set -e; rm -rf $DEST.new; mkdir -p $DEST.new; tar -xf - -C $DEST.new;
    rm -rf $DEST.old; [ -d $DEST ] && mv $DEST $DEST.old; mv $DEST.new $DEST; rm -rf $DEST.old"
rpc true
echo "deployed $ID to $HOST and restarted the service"
