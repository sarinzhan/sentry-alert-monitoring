"""/activity <msisdn> [1|3|6] — что происходило с абонентом за последние N часов.

Сервисы шлют в Sentry только ошибки (транзакций нет), поэтому хронология
строится по ошибкам; действия до сбоя достаются из breadcrumbs событий. LLM
подводит итог: что абонент пытался сделать и на что натыкался.
"""
import datetime

from app.config import TZ_OFFSET_HOURS, log
from app.services.sentry_api import discover_error_hint
from app.services.sentry_tools import fmt_event_details
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of, llm_cost_line

MAX_LINES = 25
TITLE_MAX = 80
DETAIL_EVENTS = 3
ANSWER_MAX = 1800     # summary cap; the timeline needs room in the same message

USAGE = ("Использование: <code>/activity &lt;msisdn&gt; [1|3|6]</code> — "
         "часы, по умолчанию 1.")

PROMPT = (
    "You are the assistant inside a Sentry monitoring Telegram bot. Below are "
    "ALL error events of one mobile subscriber (msisdn {msisdn}) over the last "
    "{hours}h, plus full details (stack, breadcrumbs — the user's preceding "
    "actions) of the most recent ones. Note: only errors reach Sentry, so "
    "successful actions are visible only through breadcrumbs.\n\n"
    "Events (oldest first, times UTC):\n{events}\n\n"
    "Details of the most recent event(s):\n{details}\n\n"
    "Summarize in Russian, plain text, at most 10 lines:\n"
    "- что абонент пытался сделать (по breadcrumbs/запросам);\n"
    "- с какими ошибками столкнулся, в каких сервисах, сколько раз "
    "(сгруппируй повторы);\n"
    "- есть ли закономерность (одна и та же операция падает подряд и т.п.)."
)


def _local(ts):
    """'2026-08-04T09:31:02+00:00' -> local 'HH:MM:SS'."""
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (dt + datetime.timedelta(hours=TZ_OFFSET_HOURS)).strftime("%H:%M:%S")
    except ValueError:
        return str(ts)[11:19]


async def on_activity(update, ctx):
    deps = deps_of(ctx)
    if not ctx.args:
        return await reply(update, USAGE)
    if not deps.sentry.enabled:
        return await reply(update, "Sentry API не настроен (SENTRY_API_TOKEN).")
    msisdn = ctx.args[0].strip()
    try:
        hours = int(ctx.args[1]) if len(ctx.args) > 1 else 1
    except ValueError:
        return await reply(update, USAGE)
    if not 1 <= hours <= 48:
        return await reply(update, USAGE)

    await reply(update, "🔎 собираю хронологию…")
    try:
        key, events = await deps.sentry.events_for_user(
            msisdn, stats_period=f"{hours}h")
    except Exception as e:
        return await reply(update, f"⚠️ Sentry Discover недоступен: {esc(discover_error_hint(e))}")
    if not events:
        from app.config import SENTRY_MSISDN_FIELDS
        return await reply(update,
            f"У абонента <code>{esc(msisdn)}</code> за последние {hours}ч ошибок "
            f"в Sentry нет (искал по: {esc(', '.join(SENTRY_MSISDN_FIELDS))}).")

    ordered = list(reversed(events))            # Discover returns newest first
    head = (f"📊 <b>{esc(msisdn)}</b> · последние {hours}ч · "
            f"{len(events)} ошибок (поле <code>{esc(key)}</code>, время "
            f"UTC{TZ_OFFSET_HOURS:+g}):")
    lines = [head]
    for ev in ordered[-MAX_LINES:]:
        title = (ev.get("title") or ev.get("message") or "?")[:TITLE_MAX]
        lines.append(f"{esc(_local(ev.get('timestamp')))} · "
                     f"[{esc(ev.get('project') or '?')}] · ❌ {esc(title)}")
    if len(ordered) > MAX_LINES:
        lines.insert(1, f"… показаны последние {MAX_LINES}")

    # the timeline goes out on its own — the LLM summary follows as a second
    # message, so together they can't hit Telegram's 4096-char cap
    await reply(update, "\n".join(lines))
    if not deps.llm.enabled:
        return

    details = []
    for ev in events[:DETAIL_EVENTS]:
        try:
            full = await deps.sentry.event_details(ev.get("project"), ev.get("id"))
            details.append(fmt_event_details(full))
        except Exception as e:
            log.warning("event details failed event=%s: %s", ev.get("id"), e)
    ev_lines = [f"{(ev.get('timestamp') or '').replace('T', ' ')[:19]}  "
                f"[{ev.get('project')}]  {ev.get('title') or ev.get('message')}"
                for ev in ordered]
    rec = await deps.llm.complete(PROMPT.format(
        msisdn=msisdn, hours=hours, events="\n".join(ev_lines),
        details=("\n---\n".join(details) or "—")))
    if rec and rec.text:
        llm_id = deps.llm_audit.put("activity", rec,
                                    chat_id=update.effective_chat.id)
        await reply(update, f"🤖 {esc(rec.text)[:ANSWER_MAX]}\n"
                            f"{llm_cost_line(rec, llm_id)}")
