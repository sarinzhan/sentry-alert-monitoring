"""Shared helpers for command handlers: reply, chat/thread extraction, deps access."""
from telegram import Update
from telegram.error import TelegramError

from app.config import log


def _preview(html: str, limit=300):
    """One-line, truncated view of an outgoing reply for the log."""
    s = " ".join(str(html).split())
    return s[:limit] + ("…" if len(s) > limit else "")


async def reply(update: Update, html: str):
    """Reply in the same chat/forum-topic as the incoming command."""
    try:
        await update.effective_message.reply_html(html, disable_web_page_preview=True)
        log.info("reply chat=%s: %s", update.effective_chat.id, _preview(html))
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


def llm_cost_line(rec, llm_id=None):
    """The italic cost/tokens footer with the audit id, for LLM-backed replies."""
    from app.services.llm import money
    toks = f"{rec.in_tokens} in / {rec.out_tokens} out"
    line = f"💰 {money(rec.cost)} · {toks}" if rec.cost else f"💰 {toks}"
    if llm_id:
        line += f" · 🔍 <code>/llm {llm_id}</code>"
    return f"<i>{line}</i>"
