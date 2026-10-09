"""Action registry: one table of names, argument parsing, and handlers.

Every entry point (RunScript, keymaps, skins, JSON-RPC NotifyAll, the UI)
uses these names. Parsing happens before anything is sent to the TV.
This module must not import aiowebostv, so RunScript stays cheap.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import BadRequest

if TYPE_CHECKING:
    from .tv import TvController

Parser = Callable[[list[Any]], dict[str, Any]]
Handler = Callable[["TvController", dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Action:
    name: str
    parse: Parser
    run: Handler
    # True when the caller needs data back (UI lists, command results).
    returns_data: bool = False
    usage: str = ""


def _text(args: list[Any]) -> str:
    # RunScript splits its parameters on commas; rejoin free text.
    return ",".join(str(a) for a in args).strip()


def _no_args(args: list[Any]) -> dict[str, Any]:
    if args:
        raise BadRequest("takes no arguments")
    return {}


def _choice(*choices: str, default: str | None = None) -> Parser:
    def parse(args: list[Any]) -> dict[str, Any]:
        if not args and default is not None:
            return {"value": default}
        if len(args) != 1:
            raise BadRequest(f"expected one of {', '.join(choices)}")
        value = str(args[0]).strip().lower()
        if value not in choices:
            raise BadRequest(f"expected one of {', '.join(choices)}, got {value!r}")
        return {"value": value}

    return parse


def _required_text(what: str) -> Parser:
    def parse(args: list[Any]) -> dict[str, Any]:
        value = _text(args)
        if not value:
            raise BadRequest(f"missing {what}")
        return {"value": value}

    return parse


def _parse_volume(args: list[Any]) -> dict[str, Any]:
    if len(args) != 1:
        raise BadRequest("expected up, down, or 0-100")
    value = str(args[0]).strip().lower()
    if value in ("up", "down"):
        return {"value": value}
    try:
        level = int(value)
    except ValueError:
        raise BadRequest(f"bad volume {value!r}") from None
    if not 0 <= level <= 100:
        raise BadRequest("volume must be 0-100")
    return {"value": level}


def _parse_json_object(text: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        raise BadRequest(f"{what} is not valid JSON") from None
    if not isinstance(value, dict):
        raise BadRequest(f"{what} must be a JSON object")
    return value


def _parse_app(args: list[Any]) -> dict[str, Any]:
    if not args or not str(args[0]).strip():
        raise BadRequest("missing app id or title")
    params = None
    if len(args) > 1:
        rest = args[1] if len(args) == 2 and isinstance(args[1], dict) else _text(args[1:])
        params = rest if isinstance(rest, dict) else _parse_json_object(rest, "app params")
    return {"value": str(args[0]).strip(), "params": params}


def _parse_command(args: list[Any]) -> dict[str, Any]:
    if not args or not str(args[0]).strip():
        raise BadRequest("missing SSAP uri")
    uri = str(args[0]).strip()
    if not uri.startswith("ssap://") and "://" in uri:
        raise BadRequest("uri must be ssap://...")
    payload = None
    if len(args) > 1:
        if len(args) == 2 and isinstance(args[1], dict):
            payload = args[1]
        else:
            payload = _parse_json_object(_text(args[1:]), "payload")
    return {"uri": uri, "payload": payload}


def _parse_channel(args: list[Any]) -> dict[str, Any]:
    if len(args) != 1 or not str(args[0]).strip():
        raise BadRequest("expected up, down, or a channel id")
    value = str(args[0]).strip()
    return {"value": value.lower() if value.lower() in ("up", "down") else value}


async def _status(tv: TvController, _kw: dict[str, Any]) -> dict[str, Any]:
    status = tv.status()
    if tv.connected and not status["firmware"]:
        try:
            software = await tv.software_info()
        except Exception:  # noqa: BLE001
            software = {}
        status["firmware"] = " ".join(
            str(software.get(k, "")) for k in ("product_name", "major_ver", "minor_ver")
        ).strip()
    return status


async def _catalog(tv: TvController, _kw: dict[str, Any]) -> dict[str, Any]:
    return tv.catalog()


ACTIONS: dict[str, Action] = {
    a.name: a
    for a in (
        Action("power_off", _no_args, lambda tv, kw: tv.power_off()),
        Action("power_on", _no_args, lambda tv, kw: tv.power_on()),
        Action("screen", _choice("on", "off"), lambda tv, kw: tv.screen(kw["value"] == "on"),
               usage="on|off"),
        Action("volume", _parse_volume, lambda tv, kw: tv.volume(kw["value"]),
               usage="up|down|0-100"),
        Action("mute", _choice("on", "off", "toggle", default="toggle"),
               lambda tv, kw: tv.mute(kw["value"]), usage="on|off|toggle"),
        Action("input", _required_text("input"), lambda tv, kw: tv.set_input(kw["value"]),
               usage="<input id or label>"),
        Action("app", _parse_app, lambda tv, kw: tv.launch_app(kw["value"], kw["params"]),
               usage="<app id or title>[,<json params>]"),
        Action("sound_output", _required_text("sound output"),
               lambda tv, kw: tv.sound_output(kw["value"]), usage="<output id>"),
        Action("button", _required_text("button name"), lambda tv, kw: tv.button(kw["value"]),
               usage="<button name>"),
        Action("toast", _required_text("message"), lambda tv, kw: tv.toast(kw["value"]),
               usage="<message>"),
        Action("media", _choice("play", "pause", "stop", "rewind", "fast_forward"),
               lambda tv, kw: tv.media(kw["value"]),
               usage="play|pause|stop|rewind|fast_forward"),
        Action("channel", _parse_channel, lambda tv, kw: tv.channel(kw["value"]),
               usage="up|down|<channel id>"),
        Action("command", _parse_command, lambda tv, kw: tv.command(kw["uri"], kw["payload"]),
               returns_data=True, usage="ssap://<uri>[,<json payload>]"),
        Action("repair", _no_args, lambda tv, kw: tv.repair()),
        Action("status", _no_args, _status, returns_data=True),
        Action("catalog", _no_args, _catalog, returns_data=True),
    )
}

# Handled by default.py itself, never sent to the service.
LOCAL_ACTIONS = {"ui"}


def parse(name: str, args: list[Any]) -> tuple[Action, dict[str, Any]]:
    """Validate an action name and its arguments."""
    action = ACTIONS.get(str(name).strip().lower())
    if action is None:
        raise BadRequest(f"unknown action: {name}")
    try:
        kwargs = action.parse(list(args))
    except BadRequest as err:
        usage = f" (usage: {action.name} {action.usage})" if action.usage else ""
        raise BadRequest(f"{action.name}: {err.message}{usage}") from None
    return action, kwargs


async def dispatch(tv: TvController, name: str, args: list[Any]) -> Any:
    action, kwargs = parse(name, args)
    return await action.run(tv, kwargs)
