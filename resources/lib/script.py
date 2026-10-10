"""default.py logic: RunScript argv -> IPC request, or the program UI."""

from __future__ import annotations

import json
from typing import Any

import xbmcgui

from . import actions, ipc, lang
from . import kodilog as log
from .errors import BadRequest

ADDON_NAME = "LG webOS TV"


def main(argv: list[str]) -> None:
    args = [a for a in argv[1:] if a != ""]
    if not args or args[0].strip().lower() in actions.LOCAL_ACTIONS:
        from . import ui  # noqa: PLC0415

        name = args[0].strip().lower() if args else "ui"
        if name == "find_tv":
            ui.find_tv()
        elif name == "ui" and len(args) > 1:
            ui.run_page(args[1])
        elif name == "setting":
            ui.change_setting(args[1] if len(args) > 1 else "")
        elif name == "choose_kodi_input":
            ui.choose_kodi_input(stay="stay" in args[1:])
        elif name == "choose_preset":
            ui.choose_rule_preset(args[1] if len(args) > 1 else "", stay="stay" in args[2:])
        else:
            ui.run()
        return
    run_action(args[0], args[1:])


def run_action(name: str, args: list[Any]) -> dict[str, Any] | None:
    try:
        action, _kwargs = actions.parse(name, args)
    except BadRequest as err:
        log.warning(err.message)
        notify_error(err.message)
        return None

    if action.name == "repair":
        reply = ipc.call(action.name, args)
        if reply.get("ok"):
            xbmcgui.Dialog().notification(ADDON_NAME, lang.get(lang.REPAIR_STARTED))
        else:
            notify_error(reply.get("message", ""))
        return reply

    if not action.returns_data:
        ipc.send(action.name, args)
        return None

    reply = ipc.call(action.name, args)
    if not reply.get("ok"):
        notify_error(reply.get("message", ""))
        return reply
    if action.name == "status":
        from . import ui  # noqa: PLC0415

        ui.show_status(reply.get("result") or {})
    else:
        xbmcgui.Dialog().textviewer(
            lang.get(lang.COMMAND_RESULT), json.dumps(reply.get("result"), indent=2)
        )
    return reply


def notify_error(message: str) -> None:
    xbmcgui.Dialog().notification(
        ADDON_NAME, message or lang.get(lang.ACTION_FAILED), xbmcgui.NOTIFICATION_ERROR
    )
