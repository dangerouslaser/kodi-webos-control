"""Fake xbmcgui module."""

from __future__ import annotations

import threading
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
contextmenus: list[list[str]] = []
modal_windows: list[WindowDialog] = []


def _reset() -> None:
    _windows.clear()
    notifications.clear()
    select_answers.clear()
    input_answers.clear()
    selects.clear()
    textviewers.clear()
    writes.clear()
    contextmenus.clear()
    for window in list(modal_windows):
        window.close()
    modal_windows.clear()


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


class Action:
    def __init__(self, action_id: int) -> None:
        self._id = action_id

    def getId(self) -> int:  # noqa: N802
        return self._id


class WindowDialog:
    """doModal blocks until close(); tests send actions with press()."""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self.shown = threading.Event()
        self._created_in = threading.current_thread()

    def doModal(self) -> None:  # noqa: N802
        modal_windows.append(self)
        self.shown.set()
        self._closed.wait(10)

    def close(self) -> None:
        self._closed.set()
        if self in modal_windows:
            modal_windows.remove(self)

    def onAction(self, action: Action) -> None:  # noqa: N802
        pass

    def press(self, action_id: int) -> None:
        self.onAction(Action(action_id))


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

    def contextmenu(self, items: list[str]) -> int:
        contextmenus.append([str(i) for i in items])
        return select_answers.pop(0) if select_answers else -1

    def ok(self, heading: str, message: str) -> bool:
        textviewers.append((heading, message))
        return True
