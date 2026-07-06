"""
ChatBotHandler — owns the python-telegram-bot Application.

Responsibilities:
  - run the bot (polling or via webhook updates fed in from the controller)
  - handle the /start command (reply with chat id + topic id)
  - send outgoing messages (used by the Sentry path too)
"""
import ssl
import asyncio
import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from config import (
    TELEGRAM_CA_BUNDLE, TELEGRAM_SSL_INSECURE,
    MUTE_MAX_DAYS, PROJECT_MUTE_MAX_DAYS, ALERT_STATUSES,
    params_summary, parse_duration, log,
)
from utils import esc


HELP_TEXT = (
    "<b>Подписки этого чата</b>\n"
    "<code>/subscribe &lt;проект|all&gt;</code> · <code>/unsubscribe &lt;проект|all&gt;</code> — "
    "подписать/отписать этот чат (по умолчанию алерты не приходят)\n"
    "<code>/subscriptions</code> · <code>/projects</code> — подписки чата · все проекты\n"
    "<code>/alerts &lt;new ongoing escalating|all&gt;</code> — какие статусы получать (по умолч. все)\n"
    "<code>/set &lt;параметр&gt; &lt;значение&gt;</code> · <code>/set reset</code> — правила этого чата "
    "(<code>/params</code> — текущие). Параметры: ongoing, critical_window, "
    "critical_threshold, affected_users, critical_ratelimit, stat_windows\n"
    "<b>Ошибки</b>\n"
    "<code>/status &lt;id&gt;</code> — статус ошибки (счётчики, последний алерт, мьют)\n"
    "<code>/ai &lt;id&gt;</code> — спросить AI: причина и фикс\n"
    "<code>/mute &lt;id&gt; &lt;дней&gt;</code> — отсрочить ошибку (макс "
    f"{MUTE_MAX_DAYS} дн.); или ответом на алерт: <code>/mute &lt;дней&gt;</code>\n"
    "<code>/unmute &lt;id&gt;</code> · <code>/muted</code> — снять мьют · список замьюченных\n"
    "<code>/mute_project &lt;проект&gt; &lt;дней&gt;</code> — отключить проект (макс "
    f"{PROJECT_MUTE_MAX_DAYS} дн.)\n"
    "<code>/unmute_project &lt;проект&gt;</code> — снять мьют проекта\n"
    "<code>/watch add|del &lt;текст&gt; [проект]</code> · <code>/watched</code> — force-send по тексту\n"
    "<code>/map &lt;vcs_author&gt; @&lt;tg&gt;</code> · <code>/map del|list</code> — автор коммита → Telegram\n"
    "<b>id</b> — короткий код <code>#abcdef</code> из строки 2 алерта."
)

# /set <name> -> (chat_rules column, value parser). Mirrors the /params lines.
RULE_KEYS = {
    "ongoing":            ("ongoing_sec", "duration"),
    "critical_window":    ("critical_window_sec", "duration"),
    "critical_threshold": ("critical_threshold", "int"),
    "affected_users":     ("affected_user_threshold", "int"),
    "critical_ratelimit": ("critical_ratelimit_sec", "duration"),
    "stat_windows":       ("stat_windows", "windows"),
}


def _fmt_ts(ts):
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _win_label(sec):
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec % n == 0:
            return f"{sec // n}{unit}"
    return f"{sec}s"

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
        self.app.add_handler(CommandHandler("start", self.on_start))
        self._sentry = None

    def attach_commands(self, sentry):
        """Wire the /status /mute /mute_project /watch commands to the data layer.
        Called from the controller after the SentryEventHandler exists."""
        self._sentry = sentry
        self.app.add_handler(CommandHandler("help", self.on_help))
        self.app.add_handler(CommandHandler("params", self.on_params))
        self.app.add_handler(CommandHandler("subscribe", self.on_subscribe))
        self.app.add_handler(CommandHandler("unsubscribe", self.on_unsubscribe))
        self.app.add_handler(CommandHandler("subscriptions", self.on_subscriptions))
        self.app.add_handler(CommandHandler("subs", self.on_subscriptions))
        self.app.add_handler(CommandHandler("alerts", self.on_alerts))
        self.app.add_handler(CommandHandler("set", self.on_set))
        self.app.add_handler(CommandHandler("status", self.on_status))
        self.app.add_handler(CommandHandler("ai", self.on_ai))
        self.app.add_handler(CommandHandler("mute", self.on_mute))
        self.app.add_handler(CommandHandler("unmute", self.on_unmute))
        self.app.add_handler(CommandHandler("muted", self.on_muted))
        self.app.add_handler(CommandHandler("mute_project", self.on_mute_project))
        self.app.add_handler(CommandHandler("unmute_project", self.on_unmute_project))
        self.app.add_handler(CommandHandler("projects", self.on_projects))
        self.app.add_handler(CommandHandler("watch", self.on_watch))
        self.app.add_handler(CommandHandler("watched", self.on_watched))
        self.app.add_handler(CommandHandler("map", self.on_map))

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

    # ------------------------------------------------------------- commands
    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Welcome the chat and point it at /subscribe. Alerts are opt-in per chat:
        nothing is sent here until the chat subscribes to at least one project.
        """
        chat_id = update.effective_chat.id
        lines = [
            "👋 <b>Sentry alerts bot.</b> Этот чат пока не получает алерты.",
            "",
            "1) <code>/subscribe &lt;проект|all&gt;</code> — подписаться (см. <code>/projects</code>)",
            "2) <code>/alerts new ongoing escalating|all</code> — какие статусы (по умолч. все)",
            "3) <code>/set &lt;параметр&gt; &lt;значение&gt;</code> — правила чата (<code>/params</code>)",
            "",
            "Подписки этого чата: <code>/subscriptions</code> · все команды: <code>/help</code>",
            f"<i>chat id:</i> <code>{esc(chat_id)}</code>",
        ]
        await self._reply(update, "\n".join(lines))
        log.info("/start chat_id=%s", chat_id)

    # ---------------------------------------------------- interactive commands
    @staticmethod
    async def _reply(update: Update, html: str):
        """Reply in the same chat/forum-topic as the incoming command."""
        try:
            await update.effective_message.reply_html(html, disable_web_page_preview=True)
        except TelegramError as e:
            log.error("command reply failed: %s", e)

    @staticmethod
    def _chat_of(update: Update):
        """(chat_id, thread_id) of the incoming command; thread only for forum topics."""
        msg = update.effective_message
        chat_id = update.effective_chat.id
        thread_id = msg.message_thread_id if (msg and msg.is_topic_message) else None
        return chat_id, thread_id

    async def on_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self._reply(update, HELP_TEXT)

    async def on_params(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id, _ = self._chat_of(update)
        rules = self._sentry.effective_rules(chat_id)
        await self._reply(update, "<b>Параметры этого чата</b>\n<pre>"
                          + esc(params_summary(rules)) + "</pre>")

    async def on_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await self._reply(update, "Использование: <code>/status &lt;id&gt;</code>")
        chat_id, _ = self._chat_of(update)
        windows = self._sentry.effective_rules(chat_id)["stat_windows"]
        info = self._sentry.issue_status(ctx.args[0], windows)
        if not info:
            return await self._reply(update, f"Ошибка <code>{esc(ctx.args[0])}</code> не найдена.")
        labels = "/".join(_win_label(w) for w in windows)
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

    async def on_ai(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await self._reply(update, "Использование: <code>/ai &lt;id&gt;</code>")
        await self._reply(update, "🤖 анализирую…")
        res = await self._sentry.analyze_ref(ctx.args[0])
        if res is None:
            return await self._reply(update, f"Ошибка <code>{esc(ctx.args[0])}</code> не найдена.")
        await self._reply(update, res)

    async def on_unmute(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await self._reply(update, "Использование: <code>/unmute &lt;id&gt;</code>")
        info = self._sentry.unmute_issue(ctx.args[0])
        if not info:
            return await self._reply(update, f"Ошибка <code>{esc(ctx.args[0])}</code> не найдена.")
        await self._reply(update, f"Мьют снят с ошибки "
                                  f"<code>#{esc(info['short'] or info['issue_id'])}</code>.")

    async def on_muted(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        rows = self._sentry.list_muted_issues()
        if not rows:
            return await self._reply(update, "Замьюченных ошибок нет.")
        lines = ["<b>Замьюченные ошибки:</b>"]
        for issue_id, short, title, until, by in rows:
            sid = esc(short or issue_id)
            t = esc((title or "")[:60])
            who = f" · {esc(by)}" if by else ""
            lines.append(f"<code>#{sid}</code> {t} — до {_fmt_ts(until)}{who}")
        await self._reply(update, "\n".join(lines))

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

    async def on_unmute_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await self._reply(update, "Использование: <code>/unmute_project &lt;проект&gt;</code>")
        n = self._sentry.unmute_project(ctx.args[0])
        await self._reply(update, "Мьют проекта снят." if n else "Такой мьют проекта не найден.")

    async def on_projects(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        rows = self._sentry.projects_overview()
        if not rows:
            return await self._reply(update, "Проекты не сконфигурированы "
                                             "(SENTRY_PROJECTS / GITLAB_PROJECTS).")
        chat_id, _ = self._chat_of(update)
        subs = set(self._sentry.list_subscriptions(chat_id))
        all_sub = "*" in subs
        lines = ["<b>Проекты</b> (✅ = этот чат подписан):"]
        for r in rows:
            name = esc(r["name"] or "?")
            repo = f" → <code>{esc(r['repo'])}</code>" if r["repo"] else ""
            mute = f" · 🔕 до {_fmt_ts(r['muted_until'])}" if r["muted_until"] else ""
            mark = "✅ " if (all_sub or str(r["id"]) in subs or (r["name"] and r["name"] in subs)) else ""
            lines.append(f"{mark}<code>{esc(r['id'])}</code> {name}{repo}{mute}")
        lines.append("\nПодписаться: <code>/subscribe &lt;id|имя|all&gt;</code>")
        await self._reply(update, "\n".join(lines))

    # ------------------------------------------------- per-chat subscriptions
    async def on_subscribe(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        by = update.effective_user.full_name if update.effective_user else "?"
        if not ctx.args:
            return await self._reply(update, "Использование: <code>/subscribe &lt;проект|all&gt;</code> "
                                             "(id, имя или <code>all</code>). Список: <code>/projects</code>")
        chat_id, thread_id = self._chat_of(update)
        done = []
        for a in ctx.args:
            proj = "*" if a.lower() in ("all", "*", "все") else a
            if self._sentry.subscribe(chat_id, proj, thread_id, by):
                done.append("все проекты" if proj == "*" else proj)
        where = " (тема этой ветки)" if thread_id else ""
        await self._reply(update, f"✅ Подписка добавлена: <b>{esc(', '.join(done))}</b>{where}. "
                                  f"Статусы: используйте <code>/alerts</code>, правила: <code>/params</code>.")

    async def on_unsubscribe(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await self._reply(update, "Использование: <code>/unsubscribe &lt;проект|all&gt;</code>")
        chat_id, _ = self._chat_of(update)
        n = 0
        for a in ctx.args:
            proj = "*" if a.lower() in ("all", "*", "все") else a
            n += self._sentry.unsubscribe(chat_id, proj)
        await self._reply(update, f"Отписка: удалено записей — {n}." if n
                          else "Таких подписок не найдено.")

    async def on_subscriptions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id, _ = self._chat_of(update)
        subs = self._sentry.list_subscriptions(chat_id)
        if not subs:
            return await self._reply(update, "Этот чат не подписан ни на один проект. "
                                             "<code>/subscribe &lt;проект|all&gt;</code>")
        body = "\n".join("• все проекты" if s == "*" else f"• <code>{esc(s)}</code>" for s in subs)
        rules = self._sentry.effective_rules(chat_id)
        st = "all" if not rules.get("statuses") else "/".join(
            s for s in ALERT_STATUSES if s in rules["statuses"])
        await self._reply(update, f"<b>Подписки этого чата:</b>\n{body}\n\nСтатусы: <b>{esc(st)}</b>")

    async def on_alerts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id, _ = self._chat_of(update)
        if not ctx.args:
            rules = self._sentry.effective_rules(chat_id)
            st = "all" if not rules.get("statuses") else "/".join(
                s for s in ALERT_STATUSES if s in rules["statuses"])
            return await self._reply(update, f"Текущие статусы: <b>{esc(st)}</b>\n"
                                     "Изменить: <code>/alerts new ongoing escalating</code> или "
                                     "<code>/alerts all</code>.")
        toks = [a.lower() for a in ctx.args]
        if "all" in toks:
            self._sentry.set_statuses(chat_id, None)
            return await self._reply(update, "✅ Этот чат получает <b>все</b> статусы.")
        picked = {t for t in toks if t in ALERT_STATUSES}
        if not picked:
            return await self._reply(update, "Допустимо: <code>new</code>, <code>ongoing</code>, "
                                     "<code>escalating</code> или <code>all</code>.")
        self._sentry.set_statuses(chat_id, picked)
        await self._reply(update, "✅ Статусы этого чата: <b>"
                          + esc("/".join(s for s in ALERT_STATUSES if s in picked)) + "</b>.")

    async def on_set(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id, _ = self._chat_of(update)
        args = ctx.args
        if args and args[0].lower() == "reset":
            self._sentry.reset_rules(chat_id)
            return await self._reply(update, "✅ Правила этого чата сброшены к значениям по умолчанию.")
        if len(args) < 2 or args[0] not in RULE_KEYS:
            keys = ", ".join(f"<code>{esc(k)}</code>" for k in RULE_KEYS)
            return await self._reply(update, "Использование: <code>/set &lt;параметр&gt; &lt;значение&gt;</code> "
                                     f"или <code>/set reset</code>.\nПараметры: {keys}\n"
                                     "Примеры: <code>/set ongoing 12h</code> · "
                                     "<code>/set critical_threshold 15</code> · "
                                     "<code>/set stat_windows 12h/6h/10m</code>")
        column, kind = RULE_KEYS[args[0]]
        raw = args[1]
        if kind == "duration":
            val = parse_duration(raw)
            if val is None or val <= 0:
                return await self._reply(update, "Нужна длительность, напр. <code>12h</code>, "
                                         "<code>10m</code>, <code>30s</code>.")
        elif kind == "int":
            try:
                val = int(raw)
                assert val >= 0
            except (ValueError, AssertionError):
                return await self._reply(update, "Нужно неотрицательное целое число.")
        else:  # windows: "12h/6h/10m"
            parts = [parse_duration(x) for x in raw.replace(",", "/").split("/") if x.strip()]
            if len(parts) != 3 or any(v is None or v <= 0 for v in parts):
                return await self._reply(update, "Нужно ровно 3 окна, напр. "
                                         "<code>12h/6h/10m</code>.")
            val = ",".join(str(v) for v in parts)
        self._sentry.set_rule(chat_id, column, val)
        rules = self._sentry.effective_rules(chat_id)
        await self._reply(update, f"✅ <code>{esc(args[0])}</code> обновлён для этого чата.\n<pre>"
                          + esc(params_summary(rules)) + "</pre>")

    async def _list_keywords_reply(self, update):
        rows = self._sentry.list_keywords()
        if not rows:
            return await self._reply(update, "Список ключевых слов пуст.")
        body = "\n".join(f"• <code>{esc(t)}</code>" + (f" [{esc(pr)}]" if pr else " [все]")
                         for t, pr in rows)
        await self._reply(update, "<b>Ключевые слова (force-send):</b>\n" + body)

    async def on_watched(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self._list_keywords_reply(update)

    async def on_map(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        by = update.effective_user.full_name if update.effective_user else "?"
        args = ctx.args
        sub = args[0].lower() if args else "list"
        if sub == "list":
            rows = self._sentry.map_list()
            if not rows:
                return await self._reply(update, "Маппинг пуст. "
                                                 "<code>/map &lt;vcs_author&gt; @&lt;tg&gt;</code>")
            body = "\n".join(f"• <code>{esc(v)}</code> → @{esc(tg)}" for v, tg in rows)
            return await self._reply(update, "<b>VCS-автор → Telegram:</b>\n" + body)
        if sub == "del" and len(args) >= 2:
            n = self._sentry.map_del(args[1])
            return await self._reply(update, "Удалено." if n else "Не найдено.")
        if sub == "add" and len(args) >= 3:
            vcs, tg = args[1], args[2]
        elif len(args) >= 2 and sub not in ("add", "del"):
            vcs, tg = args[0], args[1]
        else:
            return await self._reply(update, "Использование: "
                                             "<code>/map &lt;vcs_author&gt; @&lt;tg&gt;</code> · "
                                             "<code>/map del &lt;vcs_author&gt;</code> · "
                                             "<code>/map list</code>")
        if self._sentry.map_add(vcs, tg, by):
            return await self._reply(update, f"<code>{esc(vcs)}</code> → @{esc(tg.lstrip('@'))}")
        await self._reply(update, "Неверные аргументы.")

    async def on_watch(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        by = update.effective_user.full_name if update.effective_user else "?"
        sub = ctx.args[0].lower() if ctx.args else ""
        if sub == "list":
            return await self._list_keywords_reply(update)
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
