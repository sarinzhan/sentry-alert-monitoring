"""/ask <вопрос> — free-form question to the monitoring agent.

The agent queries Sentry (issues, events, users) and GitLab (source code) to
answer things like "почему фронт получает 500 на /api/orders?" or "какие
ошибки ловит пользователь 12345 за 24h?".
"""
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of

# Telegram caps a message at 4096 chars; leave room for the cost line + tags.
ANSWER_MAX = 3800


async def on_ask(update, ctx):
    q = " ".join(ctx.args) if ctx.args else ""
    if not q.strip():
        return await reply(update, "Использование: <code>/ask &lt;вопрос&gt;</code>\n"
                                   "Например: <code>/ask какие ошибки у пользователя "
                                   "12345 за 24h?</code>")
    agent = deps_of(ctx).agent
    if not agent.enabled:
        return await reply(update, "LLM выключен (ENABLE_LLM=false или нет ключа/токена).")
    await reply(update, "🤖 исследую…")
    ans = await agent.ask(q)
    if ans is None:
        return await reply(update, "Не получилось получить ответ — смотрите логи.")
    await reply(update, f"{esc(ans.text)[:ANSWER_MAX]}\n<i>{ans.cost_line}</i>")
