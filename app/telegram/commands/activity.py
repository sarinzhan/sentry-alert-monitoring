"""/activity <msisdn> [1|3|6] — что происходило с абонентом за последние N часов.

Хронология из двух источников: ошибки (Discover errors) + строки логов
приложений (Sentry logs dataset — там видны и УСПЕШНЫЕ действия; msisdn ищется
полнотекстово внутри message). LLM подводит итог: что абонент пытался сделать
и на что натыкался.
"""
import datetime

from app.config import TZ_OFFSET_HOURS, SENTRY_LOGS_DATASET, log
from app.services.sentry_api import discover_error_hint
from app.services.sentry_tools import fmt_event_details, fmt_log_line
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of, llm_cost_line

MAX_LINES = 25
TITLE_MAX = 80
DETAIL_EVENTS = 3
PROMPT_LOG_LINES = 60     # log lines fed to the LLM
PROMPT_LOG_MSG = 300      # chars of one log message in the prompt
ANSWER_MAX = 3000

USAGE = ("Использование: <code>/activity &lt;msisdn&gt; [1|3|6]</code> — "
         "часы, по умолчанию 1.")

PROMPT = (
    "You are the assistant inside a Sentry monitoring Telegram bot. Below is "
    "the activity of one mobile subscriber (msisdn {msisdn}) over the last "
    "{hours}h: application LOG lines from all services (successful operations "
    "included) and Sentry ERROR events, plus full details of the most recent "
    "error(s).\n\n"
    "Log lines (oldest first, times UTC):\n{logs}\n\n"
    "Error events (oldest first, times UTC):\n{events}\n\n"
    "Details of the most recent error(s):\n{details}\n\n"
    "Summarize in Russian, plain text, at most 12 lines:\n"
    "- что абонент делал (по логам: какие операции, в каких сервисах);\n"
    "- с какими ошибками столкнулся, сколько раз (сгруппируй повторы);\n"
    "- есть ли закономерность (одна и та же операция падает подряд, действие "
    "в одном сервисе валит другой и т.п.)."
)


def _local(ts):
    """'2026-08-04T09:31:02+00:00' -> local 'HH:MM:SS'."""
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (dt + datetime.timedelta(hours=TZ_OFFSET_HOURS)).strftime("%H:%M:%S")
    except ValueError:
        return str(ts)[11:19]


def _one_line(s, limit):
    s = " ".join(str(s or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")


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
    logs = []
    if SENTRY_LOGS_DATASET:
        try:
            logs = await deps.sentry.logs_for_user(msisdn, stats_period=f"{hours}h")
        except Exception as e:
            log.warning("logs search failed msisdn=%s: %s", msisdn, e)
    if not events and not logs:
        from app.config import SENTRY_MSISDN_FIELDS
        return await reply(update,
            f"По абоненту <code>{esc(msisdn)}</code> за последние {hours}ч в "
            f"Sentry ничего нет — ни ошибок (искал по: "
            f"{esc(', '.join(SENTRY_MSISDN_FIELDS))}), ни строк в логах.")

    # one merged chronological timeline: ▫️ log line, ❌ error
    entries = []
    for ev in events:
        entries.append((ev.get("timestamp") or "", "❌", ev.get("project") or "?",
                        _one_line(ev.get("title") or ev.get("message"), TITLE_MAX)))
    for row in logs:
        entries.append((row.get("timestamp") or "",
                        "⚠️" if row.get("exception.type") else "▫️",
                        row.get("resource.service.name") or "?",
                        _one_line(row.get("message"), TITLE_MAX)))
    entries.sort(key=lambda e: e[0])

    head = (f"📊 <b>{esc(msisdn)}</b> · последние {hours}ч · "
            f"{len(events)} ошибок, {len(logs)} строк логов "
            f"(время UTC{TZ_OFFSET_HOURS:+g}):")
    lines = [head]
    if len(entries) > MAX_LINES:
        lines.append(f"… показаны последние {MAX_LINES} из {len(entries)}")
    for ts, mark, svc, txt in entries[-MAX_LINES:]:
        lines.append(f"{esc(_local(ts))} · [{esc(svc)}] {mark} {esc(txt)}")

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
                for ev in reversed(events)]
    log_lines = [fmt_log_line(r, msg_limit=PROMPT_LOG_MSG, stack_limit=200)
                 for r in reversed(logs[:PROMPT_LOG_LINES])]
    rec = await deps.llm.complete(PROMPT.format(
        msisdn=msisdn, hours=hours,
        logs=("\n".join(log_lines) or "—"),
        events=("\n".join(ev_lines) or "—"),
        details=("\n---\n".join(details) or "—")))
    if rec and rec.text:
        llm_id = deps.llm_audit.put("activity", rec,
                                    chat_id=update.effective_chat.id)
        await reply(update, f"🤖 {esc(rec.text)[:ANSWER_MAX]}\n"
                            f"{llm_cost_line(rec, llm_id)}")
