"""/ask <вопрос> — free-form question straight to the LLM.

First iteration: a plain completion, no tools — the model answers from its own
knowledge. The agentic version (Sentry + GitLab MCP tools) comes next.
"""
from app.services.llm import money
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of

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
    llm = deps_of(ctx).llm
    if not llm.enabled:
        return await reply(update, "LLM выключен (ENABLE_LLM=false или нет ключа/токена).")
    await reply(update, "🤖 думаю…")
    res = await llm.complete(PROMPT.format(q=q))
    if res is None or not res[0]:
        return await reply(update, "Не получилось получить ответ — смотрите логи.")
    text, in_tok, out_tok, cost = res
    toks = f"{in_tok} in / {out_tok} out"
    cost_line = f"💰 {money(cost)} · {toks}" if cost else f"💰 {toks}"
    await reply(update, f"🤖 {esc(text)[:ANSWER_MAX]}\n<i>{cost_line}</i>")
