"""Fake xbmcgui module."""

from __future__ import annotations

from typing import Any

NOTIFICATION_INFO = "info"
NOTIFICATION_WARNING = "warning"
NOTIFICATION_ERROR = "error"

_windows: dict[int, dict[str, str]] = {}
notifications: list[tuple[str, str, Any]] = []
select_answers: list[int] = []
input_answers: list[str] = []
selects: list[tuple[str, list[str]]] = []
textviewers: list[tuple[str, str]] = []
writes: list[tuple[str, str]] = []


def _reset() -> None:
    _windows.clear()
    notifications.clear()
    select_answers.clear()
    input_answers.clear()
    selects.clear()
    textviewers.clear()
    writes.clear()


class Window:
    def __init__(self, window_id: int = 10000) -> None:
        self._props = _windows.setdefault(window_id, {})

    def setProperty(self, key: str, value: str) -> None:  # noqa: N802
        writes.append((key, value))
        self._props[key.lower()] = value

    def getProperty(self, key: str) -> str:  # noqa: N802
        return self._props.get(key.lower(), "")

    def clearProperty(self, key: str) -> None:  # noqa: N802
        writes.append((key, ""))
        self._props.pop(key.lower(), None)


class Dialog:
    def notification(self, heading: str, message: str, icon: Any = NOTIFICATION_INFO,
                     time: int = 5000, sound: bool = True) -> None:  # noqa: A002
        notifications.append((heading, message, icon))

    def select(self, heading: str, items: list[Any], *_a: Any, **_k: Any) -> int:
        selects.append((heading, [str(i) for i in items]))
        return select_answers.pop(0) if select_answers else -1

    def input(self, heading: str, *_a: Any, **_k: Any) -> str:
        return input_answers.pop(0) if input_answers else ""

    def textviewer(self, heading: str, text: str, *_a: Any, **_k: Any) -> None:
        textviewers.append((heading, text))

    def ok(self, heading: str, message: str) -> bool:
        textviewers.append((heading, message))
        return True
