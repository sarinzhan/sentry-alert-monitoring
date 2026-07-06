"""Shared helpers for command handlers: reply, chat/thread extraction, deps access."""
from telegram import Update
from telegram.error import TelegramError

from app.config import log


async def reply(update: Update, html: str):
    """Reply in the same chat/forum-topic as the incoming command."""
    try:
        await update.effective_message.reply_html(html, disable_web_page_preview=True)
    except TelegramError as e:
        log.error("command reply failed: %s", e)


def chat_of(update: Update):
    """(chat_id, thread_id) of the incoming command; thread only for forum topics."""
    msg = update.effective_message
    chat_id = update.effective_chat.id
    thread_id = msg.message_thread_id if (msg and msg.is_topic_message) else None
    return chat_id, thread_id


def deps_of(ctx):
    """The injected Deps bundle (see app.telegram.deps)."""
    return ctx.bot_data["deps"]


def actor(update: Update):
    """Human name of whoever issued the command (for 'by' audit fields)."""
    return update.effective_user.full_name if update.effective_user else "?"
