"""Command registry — wires every per-command module into the Application.

Each command lives in its own module (grouped by command). register_all injects
the Deps bundle into bot_data and registers the handlers, each wrapped so every
invocation is logged (who, where, which args). Replies are logged in
_helpers.reply.
"""
from telegram.ext import CommandHandler

from app.config import log
from app.telegram.commands import (
    start, help as help_cmd, params, subscribe, alerts, rules_set,
    status, ai, ask, projects, watch, usermap, llm_log, request, api_doc,
    why, activity,
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
    (["ask"], ask.on_ask),
    (["llm"], llm_log.on_llm),
    (["req", "request"], request.on_req),
    (["api"], api_doc.on_api),
    (["why"], why.on_why),
    (["activity"], activity.on_activity),
    (["projects"], projects.on_projects),
    (["watch"], watch.on_watch),
    (["watched"], watch.on_watched),
    (["map"], usermap.on_map),
]


def _logged(handler):
    """Wrap a handler so every invocation lands in the log."""
    async def wrapped(update, ctx):
        msg = update.effective_message
        cmd = (msg.text or "").split()[0] if (msg and msg.text) else "?"
        user = update.effective_user.full_name if update.effective_user else "?"
        args = " ".join(ctx.args or [])
        log.info("command %s chat=%s user=%s args=%r",
                 cmd, update.effective_chat.id, user, args[:200])
        return await handler(update, ctx)
    return wrapped


def register_all(app, deps):
    """Inject deps and register every command handler."""
    app.bot_data["deps"] = deps
    for names, handler in _COMMANDS:
        app.add_handler(CommandHandler(names, _logged(handler)))
