"""Static Telegram text: the /help body and small display formatters."""
import datetime


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
    "<code>/status &lt;id&gt;</code> — статус ошибки (счётчики, последний алерт)\n"
    "<code>/ai &lt;id&gt;</code> — спросить AI: причина и фикс\n"
    "<code>/watch add|del &lt;текст&gt; [проект]</code> · <code>/watched</code> — force-send по тексту\n"
    "<code>/map &lt;vcs_author&gt; @&lt;tg&gt;</code> · <code>/map del|list</code> — автор коммита → Telegram\n"
    "<b>id</b> — короткий код <code>#abcdef</code> из строки 2 алерта."
)


def fmt_ts(ts):
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
