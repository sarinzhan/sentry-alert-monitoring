"""Command registry — wires every per-command module into the Application.

Each command lives in its own module (grouped by command). register_all injects
the Deps bundle into bot_data and registers the handlers, each wrapped so every
invocation is logged (who, where, which args). Replies are logged in
_helpers.reply.
"""
from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import CommandHandler, filters

from app.config import log
from app.telegram.commands import (
    start, help as help_cmd, params, subscribe, alerts, rules_set,
    status, ai, ask, projects, watch, usermap, llm_log, request, api_doc,
    why, activity, notes,
)

# CommandHandler's default filter only covers regular/edited messages; the bot
# also posts alerts to channels, where commands arrive as channel_post updates.
_CMD_FILTERS = filters.UpdateType.MESSAGES | filters.UpdateType.CHANNEL_POSTS

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
    (["notes"], notes.on_notes),
    (["req", "request"], request.on_req),
    (["api"], api_doc.on_api),
    (["why"], why.on_why),
    (["activity"], activity.on_activity),
    (["projects"], projects.on_projects),
    (["watch"], watch.on_watch),
    (["watched"], watch.on_watched),
    (["map"], usermap.on_map),
]

# The "/" command menu Telegram shows in a chat with the bot (setMyCommands).
_MENU = [
    ("start", "Что умеет бот и как начать"),
    ("help", "Все команды"),
    ("subscribe", "Подписать чат на проект"),
    ("unsubscribe", "Отписать чат от проекта"),
    ("subscriptions", "Подписки этого чата"),
    ("projects", "Список проектов"),
    ("alerts", "Какие типы алертов получать"),
    ("params", "Текущие правила чата"),
    ("set", "Изменить правило чата"),
    ("status", "Статус ошибки по id"),
    ("ai", "AI-разбор ошибки: причина и фикс"),
    ("ask", "Свободный вопрос AI"),
    ("req", "Все события одного запроса"),
    ("why", "Почему у абонента ошибка"),
    ("activity", "Хронология ошибок абонента"),
    ("api", "Документация эндпоинта из кода"),
    ("llm", "Детали LLM-вызова по id"),
    ("notes", "Заметки-память модели"),
    ("watch", "Force-send по ключевому слову"),
    ("watched", "Список ключевых слов"),
    ("map", "Автор коммита → @telegram"),
]


async def set_bot_commands(bot):
    """Register the command menu (the '/' button users see in a chat with the bot)."""
    try:
        await bot.set_my_commands([BotCommand(c, d) for c, d in _MENU])
        log.info("telegram command menu registered (%d commands)", len(_MENU))
    except TelegramError as e:
        log.error("set_my_commands failed: %s", e)


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


async def _on_error(update, ctx):
    """Log handler exceptions and tell the chat — otherwise a failed command
    just looks like the bot ignored it."""
    log.error("command handler error: %s", ctx.error, exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Команда завершилась с ошибкой. Попробуйте ещё раз или см. /help.")
        except TelegramError:
            pass


def register_all(app, deps):
    """Inject deps and register every command handler."""
    app.bot_data["deps"] = deps
    for names, handler in _COMMANDS:
        app.add_handler(CommandHandler(names, _logged(handler), filters=_CMD_FILTERS))
    app.add_error_handler(_on_error)
