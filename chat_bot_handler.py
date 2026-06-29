"""
ChatBotHandler — owns the python-telegram-bot Application.

Responsibilities:
  - run the bot (polling or via webhook updates fed in from the controller)
  - handle the /start command (reply with chat id + topic id)
  - send outgoing messages (used by the Sentry path too)
"""
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from config import CHAT_ID, log
from utils import esc


class ChatBotHandler:
    def __init__(self, token: str, default_chat_id=CHAT_ID):
        self._default_chat_id = default_chat_id
        self.app = Application.builder().token(token).build()
        self.app.add_handler(CommandHandler("start", self.on_start))

    @property
    def bot(self):
        return self.app.bot

    # ------------------------------------------------------------- lifecycle
    async def start(self, polling: bool = True):
        await self.app.initialize()
        await self.app.start()
        log.info("telegram bot ok: @%s", self.bot.username)
        if polling:
            # drop any existing webhook so getUpdates won't 409, then long-poll
            await self.bot.delete_webhook(drop_pending_updates=False)
            await self.app.updater.start_polling(allowed_updates=["message", "channel_post"])
            log.info("telegram polling on")

    async def stop(self):
        if self.app.updater and self.app.updater.running:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def process_update(self, data: dict):
        """Feed one raw update (from the webhook endpoint) into the bot."""
        await self.app.process_update(Update.de_json(data, self.bot))

    # ------------------------------------------------------------- sending
    async def send(self, text: str, chat_id=None, message_thread_id=None) -> bool:
        """Send one HTML message. Returns False on failure."""
        try:
            await self.bot.send_message(
                chat_id=chat_id if chat_id is not None else self._default_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                message_thread_id=message_thread_id,   # None -> normal chat / no topic
            )
            return True
        except TelegramError as e:
            log.error("telegram send failed: %s", e)
            return False

    # ------------------------------------------------------------- commands
    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Reply in the same chat (and forum topic) with the chat id and
        message_thread_id, so the user knows what to put in TELEGRAM_CHAT_ID.
        """
        msg = update.effective_message
        chat_id = update.effective_chat.id
        # forum topics carry message_thread_id; plain chats / General topic don't
        thread_id = msg.message_thread_id if msg.is_topic_message else None

        lines = [
            "✅ <b>Got it.</b> Use these for the notifier:",
            "",
            f"<b>chat id:</b> <code>{esc(chat_id)}</code>",
        ]
        if thread_id is not None:
            lines.append(f"<b>topic (message_thread_id):</b> <code>{esc(thread_id)}</code>")
            lines.append("")
            lines.append("This message came from a forum topic — set both to post here.")

        text_out = "\n".join(lines)
        # Try to reply inside the topic; if Telegram rejects the thread (closed
        # topic, etc.) retry without it so the ids still get delivered.
        sent = await self.send(text_out, chat_id=chat_id, message_thread_id=thread_id)
        if not sent and thread_id is not None:
            log.warning("topic send rejected for chat=%s thread=%s; retrying without thread",
                        chat_id, thread_id)
            await self.send(text_out, chat_id=chat_id)
        log.info("/start chat_id=%s thread_id=%s", chat_id, thread_id)
