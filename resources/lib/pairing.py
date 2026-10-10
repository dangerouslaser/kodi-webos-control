"""Client-key storage, keyed by the configured TV host.

The key lives in addon_data/client_key.json, never in settings.xml, so
settings exports and screenshots don't leak it. Keys are also stored
under "uuid:<deviceUUID>" so they follow the TV to a new IP address.
"""

from __future__ import annotations

from . import storage

KEY_FILE = "client_key.json"


class KeyStore:
    def get(self, host: str) -> str | None:
        keys = storage.read_json(KEY_FILE, {})
        value = keys.get(host) if isinstance(keys, dict) else None
        return value if isinstance(value, str) and value else None

    def set(self, host: str, key: str) -> None:
        keys = storage.read_json(KEY_FILE, {})
        if not isinstance(keys, dict):
            keys = {}
        if keys.get(host) == key:
            return
        keys[host] = key
        storage.write_json(KEY_FILE, keys)

    def delete(self, host: str) -> bool:
        keys = storage.read_json(KEY_FILE, {})
        if not isinstance(keys, dict) or host not in keys:
            return False
        del keys[host]
        storage.write_json(KEY_FILE, keys)
        return True

    def get_uuid(self, uuid: str) -> str | None:
        return self.get(f"uuid:{uuid.lower()}")

    def set_uuid(self, uuid: str, key: str) -> None:
        self.set(f"uuid:{uuid.lower()}", key)

    def is_empty(self) -> bool:
        keys = storage.read_json(KEY_FILE, {})
        return not (isinstance(keys, dict) and keys)
