"""/ai — deep root-cause investigation of one alert.

Reply to an alert message with /ai, or pass an id: /ai <short-id|issue-id>.
The agent pulls the latest event from Sentry, reads the implicated source in
GitLab (blame + diff included) and answers with Root cause / Fix.
"""
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of

ANSWER_MAX = 3800


async def on_ai(update, ctx):
    deps = deps_of(ctx)
    issue_id = None
    msg = update.effective_message
    if msg and msg.reply_to_message:
        info = deps.context.resolve_reply(msg.reply_to_message.message_id)
        issue_id = info and info["issue_id"]
    elif ctx.args:
        info = deps.issues.resolve_ref(ctx.args[0])
        issue_id = info and info["issue_id"]
    if not issue_id:
        return await reply(update, "Ответьте на алерт командой <code>/ai</code> "
                                   "или укажите id: <code>/ai &lt;id&gt;</code>")
    p = deps.context.get_ctx(issue_id)
    if p is None:
        return await reply(update, "Нет контекста для анализа — ошибка не "
                                   "приходила после запуска бота.")
    if not deps.agent.enabled:
        return await reply(update, "LLM выключен (ENABLE_LLM=false или нет ключа/токена).")
    await reply(update, "🤖 глубокий анализ…")
    ans = await deps.agent.investigate(p)
    if ans is None:
        return await reply(update, "Не получилось получить ответ — смотрите логи.")
    await reply(update, f"{esc(ans.text)[:ANSWER_MAX]}\n<i>{ans.cost_line}</i>")
