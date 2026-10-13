"""A virtual mouse wheel for Kodi (Linux uinput).

Kodi's EventServer can move its mouse but has no wheel, and every button it
sends hides the mouse. A real wheel event scrolls whatever is under the
pointer and keeps the pointer up. So while the Remote Bridge pointer shows,
its wheel goes through a virtual absolute-pointer device (like a VM's USB
tablet), which Kodi reads like any mouse (libinput on GBM, the X server on
X11). Each wheel event carries the pointer's position, so Kodi scrolls what's
under it.

Needs write access to /dev/uinput (Kodi runs as root on LibreELEC; a container
needs the device passed in). Without it, open() returns False and the wheel
works as a button.
"""

from __future__ import annotations

import os
import struct
import time

from . import kodilog as log

DEVICE = "/dev/uinput"
NAME = b"Remote Bridge wheel"
# ioctls from linux/uinput.h.
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045566, 0x40045567
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
SYN_REPORT = 0
ABS_X, ABS_Y = 0x00, 0x01
REL_WHEEL = 0x08
# A pointer needs buttons for udev to call it a mouse; they're never pressed.
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
ABS_MAX = 32767
BUS_VIRTUAL = 0x06
# struct input_event: struct timeval, __u16 type, __u16 code, __s32 value.
EVENT = struct.Struct("llHHi")


class VirtualWheel:
    def __init__(self, path: str = DEVICE) -> None:
        self._path = path
        self._fd: int | None = None

    def open(self) -> bool:
        """Create the device; False if this system can't."""
        if self._fd is not None:
            return True
        try:
            import fcntl  # noqa: PLC0415  (Linux only)

            fd = os.open(self._path, os.O_WRONLY | os.O_NONBLOCK)
        except (ImportError, OSError) as err:
            log.info(f"remote bridge: no virtual wheel ({err}); the wheel moves the focus")
            return False
        try:
            for ev in (EV_SYN, EV_KEY, EV_REL, EV_ABS):
                fcntl.ioctl(fd, UI_SET_EVBIT, ev)
            for key in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
                fcntl.ioctl(fd, UI_SET_KEYBIT, key)
            fcntl.ioctl(fd, UI_SET_RELBIT, REL_WHEEL)
            for axis in (ABS_X, ABS_Y):
                fcntl.ioctl(fd, UI_SET_ABSBIT, axis)
            # The legacy struct uinput_user_dev: name, input_id, ff_effects_max,
            # then absmax, absmin, absfuzz, absflat (64 each).
            absmax = [0] * 64
            absmax[ABS_X] = absmax[ABS_Y] = ABS_MAX
            os.write(fd, struct.pack("80sHHHHi", NAME, BUS_VIRTUAL, 1, 1, 1, 0)
                     + struct.pack("64i", *absmax) + bytes(64 * 4 * 3))
            fcntl.ioctl(fd, UI_DEV_CREATE)
        except OSError as err:
            os.close(fd)
            log.info(f"remote bridge: no virtual wheel ({err}); the wheel moves the focus")
            return False
        self._fd = fd
        return True

    def wheel(self, x: float, y: float, notches: int) -> bool:
        """Scroll at (x, y), fractions of the screen; notches > 0 scroll up."""
        if self._fd is None:
            return False
        events = [(EV_ABS, ABS_X, _abs(x)), (EV_ABS, ABS_Y, _abs(y)),
                  (EV_REL, REL_WHEEL, notches), (EV_SYN, SYN_REPORT, 0)]
        now = time.time()
        sec, usec = int(now), int(now % 1 * 1_000_000)
        try:
            os.write(self._fd, b"".join(EVENT.pack(sec, usec, *e) for e in events))
        except OSError as err:
            log.warning(f"remote bridge: virtual wheel failed: {err}")
            self.close()
            return False
        return True

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            import fcntl  # noqa: PLC0415

            fcntl.ioctl(fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(fd)


def _abs(fraction: float) -> int:
    return max(0, min(ABS_MAX, round(fraction * ABS_MAX)))
