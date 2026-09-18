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
    "critical_threshold, affected_users, critical_ratelimit, project_window, stat_windows\n"
    "<b>Ошибки</b>\n"
    "<code>/status &lt;id&gt;</code> — статус ошибки (счётчики, последний алерт)\n"
    "<code>/ai &lt;id&gt;</code> — спросить AI: причина и фикс\n"
    "<code>/ask &lt;вопрос&gt;</code> — свободный вопрос AI\n"
    "<b>Расследование</b>\n"
    "<code>/req &lt;request_id&gt; [период]</code> — все события одного запроса по сервисам\n"
    "<code>/why &lt;msisdn&gt; &lt;время&gt; &lt;описание&gt;</code> — почему у абонента ошибка: "
    "бизнес-логика или баг\n"
    "<code>/activity &lt;msisdn&gt; [1|3|6]</code> — хронология ошибок абонента за N часов\n"
    "<code>/api &lt;путь&gt;</code> — документация эндпоинта из кода (curl, контракт, поведение)\n"
    "<code>/llm &lt;id&gt;</code> — детали LLM-вызова по id (llm_…) из ответа бота\n"
    "<code>/notes [id | del &lt;id&gt;]</code> — заметки-память модели: смотреть и чистить\n"
    "<code>/watch add|del &lt;текст&gt; [проект]</code> · <code>/watched</code> — force-send по тексту\n"
    "<code>/map &lt;vcs_author&gt; @&lt;tg&gt;</code> · <code>/map del|list</code> — автор коммита → Telegram\n"
    "<b>id</b> — короткий код <code>#abcdef</code> из строки 2 алерта."
)


def fmt_ts(ts):
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
