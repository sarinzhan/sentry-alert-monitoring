"""/llm <id> — inspect a saved LLM call: what it got, which tools it used,
what it answered. The id (llm_a1b2c3) is printed under every LLM-backed reply;
the full untruncated record is at GET /api/llm/{id}.
"""
from collections import Counter

from app.services.llm import money
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of
from app.telegram.formatting import fmt_ts

PROMPT_PREVIEW = 900
RESPONSE_PREVIEW = 1500


async def on_llm(update, ctx):
    if not ctx.args:
        return await reply(update, "Использование: <code>/llm &lt;id&gt;</code> — "
                                   "id вида <code>llm_a1b2c3</code> из ответа бота.")
    rec = deps_of(ctx).llm_audit.get(ctx.args[0])
    if rec is None:
        return await reply(update, f"Запись <code>{esc(ctx.args[0])}</code> не найдена.")

    head = [f"🔍 <b>{esc(rec['id'])}</b> · {esc(rec['kind'])} · {fmt_ts(rec['at'])}"]
    mode = "agentic" if rec["agentic"] else "one-shot"
    head.append(f"{esc(rec['model'])} · {esc(rec['auth'] or '?')} · {mode} · "
                f"{rec['turns']} turns · {rec['duration_ms'] / 1000:.1f}s")
    toks = f"{rec['in_tokens']} in / {rec['out_tokens']} out"
    head.append(f"💰 {money(rec['cost_usd'])} · {toks}" if rec["cost_usd"] else f"💰 {toks}")
    refs = []
    if rec.get("issue_id"):
        refs.append(f"issue {esc(rec['issue_id'])}")
    if rec.get("chat_id"):
        refs.append(f"chat {esc(rec['chat_id'])}")
    if refs:
        head.append(" · ".join(refs))

    if rec["tool_calls"]:
        used = Counter(c.get("tool", "?") for c in rec["tool_calls"])
        head.append("🔧 " + ", ".join(f"{esc(n)}×{k}" for n, k in used.most_common()))
    elif rec["tools_offered"]:
        head.append("🔧 инструменты были доступны, но не использовались")

    # escape FIRST, then truncate — escaping inflates text and could push the
    # message past Telegram's 4096-char cap otherwise
    prompt, resp = esc(rec["prompt"] or ""), esc(rec["response"] or "")
    body = (
        f"\n<b>Prompt</b> ({len(rec['prompt'] or '')} симв.):\n"
        f"<pre>{prompt[:PROMPT_PREVIEW]}{'…' if len(prompt) > PROMPT_PREVIEW else ''}</pre>"
        f"\n<b>Ответ</b>:\n"
        f"<pre>{resp[:RESPONSE_PREVIEW]}{'…' if len(resp) > RESPONSE_PREVIEW else ''}</pre>"
        f"\nПолная запись: <code>GET /api/llm/{esc(rec['id'])}</code>"
    )
    await reply(update, "\n".join(head) + "\n" + body)
