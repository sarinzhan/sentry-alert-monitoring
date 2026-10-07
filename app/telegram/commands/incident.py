"""Incident subsystem: the /incidents opt-in command, the Resolved button, and
the solution-text capture.

on_incidents — /incidents on|off: toggle whether THIS chat receives incident
messages (scoped to its subscribed projects). With no args, shows current state.

on_resolve_callback — fires when someone taps «✅ Разрешить» on an incident
message. It runs the agentic verification (fix merged to main? / logs clean?) and
either closes the incident immediately (code fix confirmed) or asks the resolver
to reply with the solution text.

on_solution_text — a plain-text MessageHandler that catches that reply (keyed by
chat+user in bot_data) and finalizes the resolution.
"""
from app.config import GROUP_ENABLED, log
from app.utils import esc
from app.summaries import fmt_duration
from app.telegram.commands._helpers import reply, chat_of, deps_of

_ON = {"on", "вкл", "включить", "включи", "да", "1", "true"}
_OFF = {"off", "выкл", "выключить", "выключи", "нет", "0", "false"}


async def on_incidents(update, ctx):
    chat_id, _ = chat_of(update)
    deps = deps_of(ctx)
    rules_repo = deps.rules
    args = [a.lower() for a in (ctx.args or [])]

    if args:
        tok = args[0]
        if tok not in _ON and tok not in _OFF:
            return await reply(update, "Использование: <code>/incidents on</code> или "
                               "<code>/incidents off</code>.")
        rules_repo.set_rule(chat_id, "incident_enabled", 1 if tok in _ON else 0)
        state = "включены ✅" if tok in _ON else "выключены ⛔"
        notes = []
        if tok in _ON and not GROUP_ENABLED:
            notes.append("⚠️ Группировка инцидентов выключена глобально "
                         "(GROUP_ENABLED=false) — попросите администратора включить.")
        if tok in _ON and not deps.subscriptions.list_for(chat_id):
            notes.append("⚠️ Чат не подписан ни на один проект — инциденты приходят "
                         "только по подписанным проектам (<code>/subscribe</code>).")
        tail = ("\n" + "\n".join(notes)) if notes else ""
        return await reply(update, f"Инциденты для этого чата <b>{state}</b>.{tail}")

    r = rules_repo.effective(chat_id)
    on = r.get("incident_enabled")
    win = fmt_duration(r.get("incident_window_sec") or 0)
    lines = [
        f"Инциденты: <b>{'включены ✅' if on else 'выключены ⛔'}</b>",
        f"Порог событий: <b>{esc(r.get('incident_error_threshold'))}</b> · "
        f"порог абонентов: <b>{esc(r.get('incident_user_threshold'))}</b> · "
        f"окно/таймаут: <b>{esc(win)}</b>",
        "Переключить: <code>/incidents on</code> / <code>/incidents off</code>.",
        "Инциденты приходят по подписанным проектам (<code>/subscribe</code>); "
        "пороги меняются в веб-панели.",
    ]
    if not GROUP_ENABLED:
        lines.append("⚠️ Группировка выключена глобально (GROUP_ENABLED=false).")
    await reply(update, "\n".join(lines))


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
