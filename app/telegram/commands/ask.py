"""/ask <вопрос> — free-form question straight to the LLM.

First iteration: a plain completion, no tools — the model answers from its own
knowledge. The agentic version (Sentry + GitLab MCP tools) comes next.
"""
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of, llm_cost_line

# Telegram caps a message at 4096 chars; leave room for the cost line + tags.
ANSWER_MAX = 3800

PROMPT = (
    "You are the assistant inside a Sentry error-monitoring Telegram bot used by "
    "backend engineers. Answer the question below concisely (a few short "
    "paragraphs max), in the language it was asked in (usually Russian). "
    "Plain text only — no markdown.\n\n"
    "Question: {q}"
)


async def on_ask(update, ctx):
    q = " ".join(ctx.args).strip() if ctx.args else ""
    if not q:
        return await reply(update, "Использование: <code>/ask &lt;вопрос&gt;</code>\n"
                                   "Например: <code>/ask что значит 502 Bad Gateway "
                                   "от upstream-сервиса?</code>")
    deps = deps_of(ctx)
    if not deps.llm.enabled:
        return await reply(update, "LLM выключен (ENABLE_LLM=false или нет ключа/токена).")
    await reply(update, "🤖 думаю…")
    rec = await deps.llm.complete(PROMPT.format(q=q))
    if rec is None or not rec.text:
        return await reply(update, "Не получилось получить ответ — смотрите логи.")
    llm_id = deps.llm_audit.put("ask", rec, chat_id=update.effective_chat.id)
    await reply(update, f"🤖 {esc(rec.text)[:ANSWER_MAX]}\n{llm_cost_line(rec, llm_id)}")
