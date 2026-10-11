"""Fake xbmc module for off-Kodi tests."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR, LOGFATAL, LOGNONE = 0, 1, 2, 3, 4, 5

logs: list[tuple[int, str]] = []
jsonrpc_calls: list[dict[str, Any]] = []
info_labels: dict[str, str] = {}
builtins: list[str] = []
conditions: dict[str, bool] = {}
_monitors: list[Monitor] = []
_abort = threading.Event()


def _reset() -> None:
    logs.clear()
    jsonrpc_calls.clear()
    info_labels.clear()
    builtins.clear()
    conditions.clear()
    _monitors.clear()
    _abort.clear()


def log(msg: str, level: int = LOGDEBUG) -> None:
    logs.append((level, msg))


def executeJSONRPC(request: str) -> str:  # noqa: N802
    req = json.loads(request)
    jsonrpc_calls.append(req)
    if req.get("method") == "JSONRPC.NotifyAll":
        params = req["params"]
        data = json.dumps(params.get("data"))
        for monitor in list(_monitors):
            monitor.onNotification(params["sender"], f"Other.{params['message']}", data)
    return json.dumps({"id": req.get("id"), "jsonrpc": "2.0", "result": "OK"})


def executebuiltin(command: str, wait: bool = False) -> None:  # noqa: N802
    builtins.append(command)


def getInfoLabel(label: str) -> str:  # noqa: N802
    return info_labels.get(label, "")


def getCondVisibility(condition: str) -> bool:  # noqa: N802
    return conditions.get(condition, False)


def _request_abort() -> None:
    _abort.set()


class Monitor:
    def __init__(self) -> None:
        _monitors.append(self)

    def abortRequested(self) -> bool:  # noqa: N802
        return _abort.is_set()

    def waitForAbort(self, timeout: float | None = None) -> bool:  # noqa: N802
        return _abort.wait(timeout)

    def onNotification(self, sender: str, method: str, data: str) -> None:  # noqa: N802
        pass

    def onSettingsChanged(self) -> None:  # noqa: N802
        pass


class Player:
    def __init__(self) -> None:
        pass

    def onAVStarted(self) -> None:  # noqa: N802
        pass

    def onPlayBackStopped(self) -> None:  # noqa: N802
        pass

    def onPlayBackEnded(self) -> None:  # noqa: N802
        pass

    def onPlayBackError(self) -> None:  # noqa: N802
        pass

    def isPlayingVideo(self) -> bool:  # noqa: N802
        return bool(info_labels.get("_playing_video"))

    def isPlayingAudio(self) -> bool:  # noqa: N802
        return bool(info_labels.get("_playing_audio"))


def sleep(ms: int) -> None:
    time.sleep(ms / 1000)
