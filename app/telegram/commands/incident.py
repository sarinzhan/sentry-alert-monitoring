"""Incident Resolved button + solution-text capture.

on_resolve_callback — fires when someone taps «✅ Разрешить» on an incident
message. It runs the agentic verification (fix merged to main? / logs clean?) and
either closes the incident immediately (code fix confirmed) or asks the resolver
to reply with the solution text.

on_solution_text — a plain-text MessageHandler that catches that reply (keyed by
chat+user in bot_data) and finalizes the resolution.
"""
from app.config import log
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of


def _pending(ctx):
    return ctx.bot_data.setdefault("incident_pending", {})


async def on_resolve_callback(update, ctx):
    q = update.callback_query
    data = (q.data or "") if q else ""
    if not data.startswith("incident:resolve:"):
        return await q.answer()
    try:
        incident_id = int(data.split(":")[2])
    except (IndexError, ValueError):
        return await q.answer("Некорректный инцидент.")

    svc = deps_of(ctx).incidents
    if svc is None:
        return await q.answer("Инциденты выключены.")

    by = q.from_user.full_name if q.from_user else None
    await q.answer("⏳ Проверяю…")                  # immediate ack (verification is slow)
    log.info("incident resolve requested inc=%s by=%s", incident_id, by)

    outcome = await svc.request_resolve(incident_id, by)
    status = outcome.get("status")
    if status in (None, "gone"):
        return                                       # already resolved elsewhere
    if status == "resolved":
        return                                       # service already closed + posted summary

    # need_text: remember we're awaiting the solution note from this user here
    verdict = outcome.get("verdict") or {}
    user_id = q.from_user.id if q.from_user else None
    chat_id = q.message.chat.id if q.message else None
    _pending(ctx)[(str(chat_id), user_id)] = {
        "incident_id": incident_id, "verdict": verdict, "by": by}

    warn = "" if verdict.get("verified") else "⚠️ Автоматически подтвердить не удалось. "
    ev = f" ({esc(verdict['evidence'])})" if verdict.get("evidence") else ""
    prompt = (f"{warn}✍️ Напишите текст решения для инцидента "
              f"<code>#{incident_id}</code> — ответьте сообщением в этот чат.{ev}")
    if q.message is not None:
        await q.message.reply_html(prompt, disable_web_page_preview=True)


async def on_solution_text(update, ctx):
    pending = ctx.bot_data.get("incident_pending")
    if not pending:
        return
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or not (msg.text or "").strip():
        return
    key = (str(update.effective_chat.id), user.id)
    entry = pending.get(key)
    if not entry:
        return
    del pending[key]
    svc = deps_of(ctx).incidents
    if svc is None:
        return
    ok = await svc.finalize_resolve(entry["incident_id"], entry.get("by"),
                                    msg.text.strip(), entry.get("verdict"))
    if not ok:
        await reply(update, "Инцидент уже закрыт.")
