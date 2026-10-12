"""Where things are on screen, learned from the Remote Bridge pointer.

Kodi can't say where a control or list item is drawn. But while the pointer
moves, Kodi focuses what's under it, so each pointer position tells us where
the focused control (and the item slot it's on) sits. When the D-pad has moved
focus and the pointer comes back, it goes to the focused item's spot as last
seen, or one worked out from items next to it: lists step by a fixed pitch.

Places are keyed by (window, control, context): context is whatever moves the
control as a whole (e.g. the home screen's rows scrolling in a grouplist).
Slots are (row, column) in panels and (position, 0) in lists, counted in the
visible area, so a list scrolling under a fixed focus position doesn't move it.
"""

from __future__ import annotations

from typing import Any

Place = tuple[str, str, str]
Slot = tuple[int, int]
Point = tuple[float, float]

# A running average over about this many samples: hovering an item lands
# anywhere on it; the average drifts toward its middle.
SAMPLES = 8
# Forget everything past this many places (skins don't have that many).
MAX_PLACES = 400
MAX_SLOTS = 200


class PointerMap:
    def __init__(self, size: tuple[float, float] = (1920.0, 1080.0)) -> None:
        self._size = size
        # place -> slot -> (x, y, weight)
        self._spots: dict[Place, dict[Slot | None, tuple[float, float, int]]] = {}
        # place -> axis (0, 1) -> (dx, dy) per step along that axis
        self._pitch: dict[Place, dict[int, Point]] = {}

    def note(self, place: Place, slot: Slot | None, x: float, y: float) -> None:
        """The pointer is at x, y and Kodi has focused `slot` of `place`."""
        spots = self._spots.get(place)
        if spots is None:
            if len(self._spots) >= MAX_PLACES:
                self._spots.clear()
                self._pitch.clear()
            spots = self._spots[place] = {}
        old = spots.get(slot)
        if old is None:
            if len(spots) >= MAX_SLOTS:
                spots.clear()
            spot = (x, y, 1)
        else:
            ox, oy, n = old
            n = min(n + 1, SAMPLES)
            spot = (ox + (x - ox) / n, oy + (y - oy) / n, n)
        spots[slot] = spot
        if slot is not None:
            self._learn_pitch(place, slot, spot)

    def where(self, place: Place, slot: Slot | None) -> Point | None:
        """Best guess at where `slot` of `place` is, or None."""
        spots = self._spots.get(place)
        if not spots:
            return None
        exact = spots.get(slot)
        if exact is not None:
            return self._clamp(exact[0], exact[1])
        if slot is None:
            return None
        pitch = self._pitch.get(place, {})
        best: tuple[int, Point] | None = None
        for known, (x, y, _n) in spots.items():
            if known is None:
                continue
            steps = (slot[0] - known[0], slot[1] - known[1])
            if any(d and axis not in pitch for axis, d in enumerate(steps)):
                continue  # no idea how far that is
            for axis, d in enumerate(steps):
                if d:
                    x += d * pitch[axis][0]
                    y += d * pitch[axis][1]
            distance = abs(steps[0]) + abs(steps[1])
            if best is None or distance < best[0]:
                best = (distance, (x, y))
        return self._clamp(*best[1]) if best is not None else None

    def forget(self) -> None:
        """The layout changed (another skin, a reload)."""
        self._spots.clear()
        self._pitch.clear()

    def dump(self) -> dict[str, Any]:
        return {
            "spots": [[list(place), [_slot_out(s), x, y, n]]
                      for place, spots in self._spots.items()
                      for s, (x, y, n) in spots.items()],
            "pitch": [[list(place), axis, dx, dy]
                      for place, axes in self._pitch.items() for axis, (dx, dy) in axes.items()],
        }

    def load(self, data: Any) -> None:
        self.forget()
        try:
            for place, (slot, x, y, n) in data.get("spots", []):
                self._spots.setdefault(tuple(str(p) for p in place), {})[_slot_in(slot)] = (
                    float(x), float(y), int(n))
            for place, axis, dx, dy in data.get("pitch", []):
                self._pitch.setdefault(tuple(str(p) for p in place), {})[int(axis)] = (float(dx), float(dy))
        except (AttributeError, TypeError, ValueError):
            self.forget()

    def _learn_pitch(self, place: Place, slot: Slot, spot: tuple[float, float, int]) -> None:
        """From another slot of the same place in line with this one."""
        for known, (x, y, _n) in self._spots[place].items():
            if known is None or known == slot:
                continue
            for axis in (0, 1):
                other = 1 - axis
                steps = slot[axis] - known[axis]
                if steps and slot[other] == known[other]:
                    self._pitch.setdefault(place, {})[axis] = (
                        (spot[0] - x) / steps, (spot[1] - y) / steps)

    def _clamp(self, x: float, y: float) -> Point:
        w, h = self._size
        return min(max(x, 0.0), w - 1), min(max(y, 0.0), h - 1)


def _slot_out(slot: Slot | None) -> list[int] | None:
    return None if slot is None else list(slot)


def _slot_in(slot: Any) -> Slot | None:
    return None if slot is None else (int(slot[0]), int(slot[1]))
