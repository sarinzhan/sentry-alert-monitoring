"""
ChatBotHandler — owns the python-telegram-bot Application.

Responsibilities:
  - run the bot (polling or via webhook updates fed in from the controller)
  - handle the /start command (reply with chat id + topic id)
  - send outgoing messages (used by the Sentry path too)
"""
import ssl
import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from config import (
    CHAT_ID, CHAT_THREAD_ID, TELEGRAM_CA_BUNDLE, TELEGRAM_SSL_INSECURE,
    MUTE_MAX_DAYS, PROJECT_MUTE_MAX_DAYS, STAT_WINDOWS, params_summary, log,
)
from utils import esc


HELP_TEXT = (
    "<b>Команды</b>\n"
    "<code>/status &lt;id&gt;</code> — статус ошибки (счётчики, последний алерт, мьют)\n"
    "<code>/mute &lt;id&gt; &lt;дней&gt;</code> — отсрочить ошибку (макс "
    f"{MUTE_MAX_DAYS} дн.); или ответом на алерт: <code>/mute &lt;дней&gt;</code>\n"
    "<code>/mute_project &lt;проект&gt; &lt;дней&gt;</code> — отключить проект (макс "
    f"{PROJECT_MUTE_MAX_DAYS} дн.)\n"
    "<code>/watch add &lt;текст&gt; [проект]</code> — всегда слать при совпадении текста\n"
    "<code>/watch del &lt;текст&gt; [проект]</code> · <code>/watch list</code>\n"
    "<code>/params</code> — текущие значения параметров\n"
    "<b>id</b> — короткий код <code>#abcdef</code> из строки 2 алерта."
)


def _fmt_ts(ts):
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _win_label(sec):
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec % n == 0:
            return f"{sec // n}{unit}"
    return f"{sec}s"

# sentinel so send() can tell "caller omitted thread" (use the default topic) apart
# from "caller explicitly passed None" (post to the chat with no topic).
_UNSET = object()


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
    def __init__(self, token: str, default_chat_id=CHAT_ID, default_thread_id=CHAT_THREAD_ID):
        self._default_chat_id = default_chat_id
        self._default_thread_id = default_thread_id
        ctx = _build_ssl_context()
        self.app = (
            Application.builder()
            .token(token)
            # one request object for normal API calls, one for long-polling getUpdates
            .request(HTTPXRequest(httpx_kwargs={"verify": ctx}))
            .get_updates_request(HTTPXRequest(httpx_kwargs={"verify": ctx}))
            .build()
        )
        self.app.add_handler(CommandHandler("start", self.on_start))
        self._sentry = None

    def attach_commands(self, sentry):
        """Wire the /status /mute /mute_project /watch commands to the data layer.
        Called from the controller after the SentryEventHandler exists."""
        self._sentry = sentry
        self.app.add_handler(CommandHandler("help", self.on_help))
        self.app.add_handler(CommandHandler("params", self.on_params))
        self.app.add_handler(CommandHandler("status", self.on_status))
        self.app.add_handler(CommandHandler("mute", self.on_mute))
        self.app.add_handler(CommandHandler("mute_project", self.on_mute_project))
        self.app.add_handler(CommandHandler("watch", self.on_watch))

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
    async def send(self, text: str, chat_id=None, message_thread_id=_UNSET):
        """Send one HTML message. Returns the sent Message, or None on failure.

        Omit message_thread_id to fall back to the configured default topic;
        pass None explicitly to force posting outside any topic.
        """
        if message_thread_id is _UNSET:
            message_thread_id = self._default_thread_id
        try:
            return await self.bot.send_message(
                chat_id=chat_id if chat_id is not None else self._default_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                message_thread_id=message_thread_id,   # None -> normal chat / no topic
            )
        except TelegramError as e:
            log.error("telegram send failed: %s", e)
            return None

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
            await self.send(text_out, chat_id=chat_id, message_thread_id=None)
        log.info("/start chat_id=%s thread_id=%s", chat_id, thread_id)

    # ---------------------------------------------------- interactive commands
    @staticmethod
    async def _reply(update: Update, html: str):
        """Reply in the same chat/forum-topic as the incoming command."""
        try:
            await update.effective_message.reply_html(html, disable_web_page_preview=True)
        except TelegramError as e:
            log.error("command reply failed: %s", e)

    async def on_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self._reply(update, HELP_TEXT)

    async def on_params(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self._reply(update, "<b>Параметры</b>\n<pre>" + esc(params_summary()) + "</pre>")

    async def on_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await self._reply(update, "Использование: <code>/status &lt;id&gt;</code>")
        info = self._sentry.issue_status(ctx.args[0])
        if not info:
            return await self._reply(update, f"Ошибка <code>{esc(ctx.args[0])}</code> не найдена.")
        labels = "/".join(_win_label(w) for w in STAT_WINDOWS)
        counts = "/".join(str(c) for c in info["counts"])
        title = esc(info.get("title") or "?")
        title = f'<a href="{esc(info["url"])}">{title}</a>' if info.get("url") else title
        mute = (f"замьючено до {_fmt_ts(info['muted_until'])}"
                if info.get("muted_until") else "нет")
        lines = [
            f"#<code>{esc(info['short'])}</code> · <b>{esc(info.get('project') or '?')}</b>",
            title,
            f"события ({labels}): <b>{counts}</b>",
            f"последний алерт: {_fmt_ts(info.get('last_sent'))}",
            f"мьют: {mute}",
        ]
        await self._reply(update, "\n".join(lines))

    async def on_mute(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        by = update.effective_user.full_name if update.effective_user else "?"
        reply_to = update.effective_message.reply_to_message
        # reply form: reply to an alert with "/mute <days>"; else "/mute <id> <days>"
        if reply_to and len(ctx.args) == 1:
            info = self._sentry.resolve_reply(reply_to.message_id)
            if not info:
                return await self._reply(update, "Не нашёл ошибку для этого сообщения — "
                                                 "укажи id: <code>/mute &lt;id&gt; &lt;дней&gt;</code>")
            ref, days = info["short"] or info["issue_id"], ctx.args[0]
        elif len(ctx.args) >= 2:
            ref, days = ctx.args[0], ctx.args[1]
        else:
            return await self._reply(update, "Использование: <code>/mute &lt;id&gt; &lt;дней&gt;</code> "
                                             "или ответом на алерт: <code>/mute &lt;дней&gt;</code>")
        res = self._sentry.mute_issue(ref, days, by)
        if not res:
            return await self._reply(update, f"Не удалось: ошибка <code>{esc(ref)}</code> не найдена "
                                             f"или неверное число дней (1..{MUTE_MAX_DAYS}).")
        link = (f'<a href="{esc(res["url"])}">#{esc(res["short"])}</a>'
                if res.get("url") else f"#{esc(res['short'])}")
        await self._reply(update, f"Пользователь <b>{esc(by)}</b> отсрочил ошибку {link} "
                                  f"на {res['days']} дн. (до {_fmt_ts(res['until'])}).")

    async def on_mute_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        by = update.effective_user.full_name if update.effective_user else "?"
        if len(ctx.args) < 2:
            return await self._reply(update, "Использование: "
                                             "<code>/mute_project &lt;проект&gt; &lt;дней&gt;</code>")
        res = self._sentry.mute_project(ctx.args[0], ctx.args[1], by)
        if not res:
            return await self._reply(update, f"Неверное число дней (1..{PROJECT_MUTE_MAX_DAYS}).")
        await self._reply(update, f"Пользователь <b>{esc(by)}</b> отключил алерты проекта "
                                  f"<b>{esc(res['project'])}</b> на {res['days']} дн. "
                                  f"(до {_fmt_ts(res['until'])}).")

    async def on_watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        by = update.effective_user.full_name if update.effective_user else "?"
        sub = ctx.args[0].lower() if ctx.args else ""
        if sub == "list":
            rows = self._sentry.list_keywords()
            if not rows:
                return await self._reply(update, "Список ключевых слов пуст.")
            body = "\n".join(f"• <code>{esc(t)}</code>" + (f" [{esc(pr)}]" if pr else " [все]")
                             for t, pr in rows)
            return await self._reply(update, "<b>Ключевые слова (force-send):</b>\n" + body)
        if sub in ("add", "del") and len(ctx.args) >= 2:
            text = ctx.args[1]
            project = ctx.args[2] if len(ctx.args) >= 3 else None
            scope = f"[{esc(project)}]" if project else "[все проекты]"
            if sub == "add":
                ok = self._sentry.add_keyword(text, project, by)
                msg = (f"Добавлено ключевое слово <code>{esc(text)}</code> {scope}." if ok
                       else f"Уже есть: <code>{esc(text)}</code> {scope}.")
            else:
                n = self._sentry.del_keyword(text, project)
                msg = (f"Удалено <code>{esc(text)}</code> {scope}." if n
                       else f"Не найдено: <code>{esc(text)}</code> {scope}.")
            return await self._reply(update, msg)
        await self._reply(update, "Использование: <code>/watch add &lt;текст&gt; [проект]</code> · "
                                  "<code>/watch del &lt;текст&gt; [проект]</code> · <code>/watch list</code>")
