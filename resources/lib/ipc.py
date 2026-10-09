"""Request/reply over Kodi's notification bus (JSONRPC.NotifyAll).

Requests:  sender "service.webostv", message "request",
           data {"id", "action", "args", "notify_errors"?}
Replies:   sender "service.webostv", message "reply",
           data {"id", "ok": true, "result"} or {"id", "error", "message"}

Monitor.onNotification sees these as method "Other.request" /
"Other.reply" with data as a JSON string.
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

import xbmc

SENDER = "service.webostv"
REQUEST = "request"
REPLY = "reply"
REPLY_TIMEOUT = 5.0


def notify_all(message: str, data: Any) -> None:
    xbmc.executeJSONRPC(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "JSONRPC.NotifyAll",
        "params": {"sender": SENDER, "message": message, "data": data},
    }))


def make_request(action: str, args: list[Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex, "action": action, "args": list(args or []), **extra}


def parse_notification(sender: str, method: str, data: str) -> tuple[str, dict[str, Any]] | None:
    """Return (message, payload) for our notifications, else None."""
    if sender != SENDER:
        return None
    message = method.split(".", 1)[1] if method.startswith("Other.") else method
    if message not in (REQUEST, REPLY):
        return None
    try:
        payload = json.loads(data) if isinstance(data, str) else data
        # Some clients double-encode data as a JSON string.
        if isinstance(payload, str):
            payload = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or "id" not in payload:
        return None
    return message, payload


def reply_ok(request_id: str, result: Any = None) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result}


def reply_error(request_id: str, code: str, message: str = "") -> dict[str, Any]:
    return {"id": request_id, "ok": False, "error": code, "message": message or code}


class _ReplyMonitor(xbmc.Monitor):
    def __init__(self, request_id: str) -> None:
        super().__init__()
        self.request_id = request_id
        self.reply: dict[str, Any] | None = None
        self.event = threading.Event()

    def onNotification(self, sender: str, method: str, data: str) -> None:  # noqa: N802
        parsed = parse_notification(sender, method, data)
        if parsed and parsed[0] == REPLY and parsed[1].get("id") == self.request_id:
            self.reply = parsed[1]
            self.event.set()


def send(action: str, args: list[Any] | None = None, *, notify_errors: bool = True) -> str:
    """Fire-and-forget; the service shows a notification if it fails."""
    request = make_request(action, args, notify_errors=notify_errors)
    notify_all(REQUEST, request)
    return request["id"]


def call(action: str, args: list[Any] | None = None, timeout: float = REPLY_TIMEOUT) -> dict[str, Any]:
    """Send a request and wait for its reply.

    Returns the reply dict; on timeout returns an error reply with code
    "timeout" (the service is probably not running).
    """
    request = make_request(action, args)
    monitor = _ReplyMonitor(request["id"])
    notify_all(REQUEST, request)
    waited = 0.0
    step = 0.05
    while not monitor.event.is_set() and waited < timeout:
        if monitor.waitForAbort(step):
            break
        waited += step
    if monitor.reply is None:
        return reply_error(request["id"], "timeout", "No reply from the webOS service")
    return monitor.reply
