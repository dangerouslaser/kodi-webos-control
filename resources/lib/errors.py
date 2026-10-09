"""Errors that map onto IPC reply codes."""

from __future__ import annotations


class ActionError(Exception):
    """An action failed; ``code`` is what the IPC reply carries as ``error``."""

    code = "error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class BadRequest(ActionError):
    code = "bad_request"


class Disconnected(ActionError):
    code = "disconnected"


class CommandFailed(ActionError):
    code = "command_error"
