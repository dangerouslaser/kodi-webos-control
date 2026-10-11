"""Fake xbmcplugin module: records directory listings."""

from __future__ import annotations

from typing import Any

listings: dict[int, list[tuple[str, Any, bool]]] = {}
ended: list[int] = []


def _reset() -> None:
    listings.clear()
    ended.clear()


def addDirectoryItems(handle: int, items: list, total: int = 0) -> bool:  # noqa: N802
    listings.setdefault(handle, []).extend(items)
    return True


def setContent(handle: int, content: str) -> None:  # noqa: N802
    pass


def endOfDirectory(handle: int, succeeded: bool = True, **_k: Any) -> None:  # noqa: N802
    ended.append(handle)
