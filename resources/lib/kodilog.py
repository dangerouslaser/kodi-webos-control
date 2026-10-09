"""Logging to xbmc.log with a fixed prefix and client-key redaction."""

from __future__ import annotations

import logging
import re

import xbmc

ADDON_ID = "service.webostv"
PREFIX = f"[{ADDON_ID}] "

_KEY_RE = re.compile(r"""(["']client-key["']\s*:\s*["'])[^"']+(["'])""")
_debug_enabled = False


def redact(text: str) -> str:
    """Mask any client-key value in a log line."""
    return _KEY_RE.sub(r"\1***\2", text)


def set_debug(enabled: bool) -> None:
    """Gate DEBUG output (ours and aiowebostv's) on the debug setting."""
    global _debug_enabled  # noqa: PLW0603
    _debug_enabled = enabled
    level = logging.DEBUG if enabled else logging.INFO
    for name in ("aiowebostv", "websockets"):
        logging.getLogger(name).setLevel(level if name == "aiowebostv" else logging.WARNING)


def debug(msg: str) -> None:
    if _debug_enabled:
        # LOGINFO so it appears without Kodi's own debug logging enabled.
        xbmc.log(PREFIX + redact(msg), xbmc.LOGINFO)


def info(msg: str) -> None:
    xbmc.log(PREFIX + redact(msg), xbmc.LOGINFO)


def warning(msg: str) -> None:
    xbmc.log(PREFIX + redact(msg), xbmc.LOGWARNING)


def error(msg: str) -> None:
    xbmc.log(PREFIX + redact(msg), xbmc.LOGERROR)


class KodiLogHandler(logging.Handler):
    """Route Python ``logging`` records (aiowebostv) into xbmc.log."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = f"{PREFIX}{record.name}: {redact(self.format(record))}"
            if record.levelno >= logging.ERROR:
                level = xbmc.LOGERROR
            elif record.levelno >= logging.WARNING:
                level = xbmc.LOGWARNING
            elif record.levelno >= logging.INFO or _debug_enabled:
                level = xbmc.LOGINFO
            else:
                return
            xbmc.log(msg, level)
        except Exception:  # noqa: BLE001
            self.handleError(record)


def install_handler() -> None:
    """Attach the Kodi handler to the loggers we care about (idempotent)."""
    for name in ("aiowebostv", "websockets"):
        logger = logging.getLogger(name)
        if not any(isinstance(h, KodiLogHandler) for h in logger.handlers):
            logger.addHandler(KodiLogHandler())
        logger.propagate = False
    set_debug(_debug_enabled)
