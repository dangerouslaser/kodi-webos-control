"""Call internal luna services through the SSAP alert API.

SSAP doesn't expose luna:// services directly. The workaround (from
bscpylgtv): create an alert whose button, onclose and onfail handlers all
point at the luna URI, then close the alert immediately so the TV runs the
handler. The luna call's result is never returned, so callers must verify
the effect some other way (e.g. by reading the setting back).

Verified on webOS 10.3.0 (OLED77G5WUA, firmware 33.30.97).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

CREATE_ALERT = "system.notifications/createAlert"
CLOSE_ALERT = "system.notifications/closeAlert"
SET_SYSTEM_SETTINGS = "com.webos.settingsservice/setSystemSettings"

Request = Callable[[str, dict[str, Any] | None], Awaitable[dict[str, Any]]]


def alert_payload(uri: str, params: dict[str, Any]) -> dict[str, Any]:
    luna_uri = f"luna://{uri}"
    return {
        "message": " ",
        "buttons": [{"label": "", "onClick": luna_uri, "params": params}],
        "onclose": {"uri": luna_uri, "params": params},
        "onfail": {"uri": luna_uri, "params": params},
    }


async def call(request: Request, uri: str, params: dict[str, Any]) -> None:
    """Fire a luna call. Raises if the alert can't be created or closed."""
    created = await request(CREATE_ALERT, alert_payload(uri, params))
    alert_id = created.get("alertId")
    if not alert_id:
        error = f"createAlert returned no alertId: {created}"
        raise RuntimeError(error)
    await request(CLOSE_ALERT, {"alertId": alert_id})


async def set_picture_settings(request: Request, settings: dict[str, Any]) -> None:
    await call(request, SET_SYSTEM_SETTINGS, {"category": "picture", "settings": settings})
