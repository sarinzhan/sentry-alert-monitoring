"""ChatBotHandler — owns the python-telegram-bot Application.

Responsibilities: run the bot (polling or webhook-fed), send outgoing messages,
and hand incoming updates to the registered command handlers. Command handlers
themselves live in app.telegram.commands and are wired in by the controller.
"""
import ssl
import asyncio

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application
from telegram.request import HTTPXRequest

from app.config import TELEGRAM_CA_BUNDLE, TELEGRAM_SSL_INSECURE, log


def _build_ssl_context() -> ssl.SSLContext:
    """
    SSL context for talking to api.telegram.org, tolerant of corporate MITM proxies.

    - trusts the system/certifi store, plus an optional corporate CA bundle
    - clears VERIFY_X509_STRICT so a CA cert missing the Authority Key Identifier
      extension (common with internal CAs) doesn't fail verification on modern OpenSSL
    - optionally drops verification entirely as a last resort
    """
    ctx = ssl.create_default_context()
    if TELEGRAM_CA_BUNDLE:
        ctx.load_verify_locations(TELEGRAM_CA_BUNDLE)
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if TELEGRAM_SSL_INSECURE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ChatBotHandler:
    def __init__(self, token: str):
        ctx = _build_ssl_context()
        self.app = (
            Application.builder()
            .token(token)
            # one request object for normal API calls, one for long-polling getUpdates
            .request(HTTPXRequest(httpx_kwargs={"verify": ctx}))
            .get_updates_request(HTTPXRequest(httpx_kwargs={"verify": ctx}))
            .build()
        )

    @property
    def bot(self):
        return self.app.bot

    # ------------------------------------------------------------- lifecycle
    async def start(self, polling: bool = True):
        await self.app.initialize()
        await self.app.start()
        log.info("telegram bot ok: @%s", self.bot.username)
        if polling:
            await self._clear_webhook()
            # drop_pending_updates so a backlog delivered to the old webhook isn't replayed
            await self.app.updater.start_polling(
                allowed_updates=["message", "channel_post"], drop_pending_updates=True)
            log.info("telegram polling on")

    async def _clear_webhook(self):
        """Ensure no webhook is registered before long-polling.

        getUpdates and a webhook can't both be active — Telegram returns 409 Conflict
        ("can't use getUpdates method while webhook is active"). A single delete can lose
        the race (or silently fail behind a proxy), so verify and retry a few times.
        """
        for attempt in range(1, 6):
            try:
                info = await self.bot.get_webhook_info()
                if not info.url:
                    if attempt > 1:
                        log.info("telegram webhook cleared")
                    return
                log.warning("telegram webhook active (%s); deleting to enable polling (try %d)",
                            info.url, attempt)
                await self.bot.delete_webhook(drop_pending_updates=True)
            except TelegramError as e:
                log.error("telegram delete_webhook failed (try %d): %s", attempt, e)
            await asyncio.sleep(1)
        log.error("telegram webhook still set after retries — polling may 409. "
                  "Set TELEGRAM_POLLING=false to use the /telegram webhook instead.")

    async def stop(self):
        if self.app.updater and self.app.updater.running:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def process_update(self, data: dict):
        """Feed one raw update (from the webhook endpoint) into the bot."""
        await self.app.process_update(Update.de_json(data, self.bot))

    # ------------------------------------------------------------- sending
    async def send(self, text: str, chat_id, message_thread_id=None):
        """Send one HTML message to an explicit chat. Returns the Message, or None.

        message_thread_id targets a forum topic; None posts to the chat with no topic.
        """
        if chat_id is None:
            log.error("telegram send skipped: no chat_id given")
            return None
        try:
            return await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                message_thread_id=message_thread_id,   # None -> normal chat / no topic
            )
        except TelegramError as e:
            log.error("telegram send failed: %s", e)
            return None
