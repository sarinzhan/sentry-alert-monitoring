"""Command registry — wires every per-command module into the Application.

Each command lives in its own module (grouped by command). register_all injects
the Deps bundle into bot_data and registers the handlers.
"""
from telegram.ext import CommandHandler

from app.telegram.commands import (
    start, help as help_cmd, params, subscribe, alerts, rules_set,
    status, ai, projects, watch, usermap,
)

# (command names, handler). A list of names registers aliases to one handler.
_COMMANDS = [
    (["start"], start.on_start),
    (["help"], help_cmd.on_help),
    (["params"], params.on_params),
    (["subscribe"], subscribe.on_subscribe),
    (["unsubscribe"], subscribe.on_unsubscribe),
    (["subscriptions", "subs"], subscribe.on_subscriptions),
    (["alerts"], alerts.on_alerts),
    (["set"], rules_set.on_set),
    (["status"], status.on_status),
    (["ai"], ai.on_ai),
    (["projects"], projects.on_projects),
    (["watch"], watch.on_watch),
    (["watched"], watch.on_watched),
    (["map"], usermap.on_map),
]


def register_all(app, deps):
    """Inject deps and register every command handler."""
    app.bot_data["deps"] = deps
    for names, handler in _COMMANDS:
        app.add_handler(CommandHandler(names, handler))
