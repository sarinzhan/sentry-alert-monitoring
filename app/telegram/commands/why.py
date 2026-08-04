"""/why <msisdn> <время> <описание> — почему у абонента возникла ошибка.

Ищет ошибки абонента и строки логов приложений в Sentry вокруг указанного
времени (±45 мин, при пустом результате автоматически ±3 ч), отдаёт их
агентному LLM с инструментами GitLab + Sentry и просит вердикт: бизнес-логика
(система сработала как задумано) или ошибка в коде. Работает и без ошибок —
отказ по бизнес-правилу часто виден только в логах.
"""
import re
import datetime

from app.config import (
    GITLAB_PROJECTS, GITLAB_REF, ENABLE_LLM_TOOLS, TZ_OFFSET_HOURS,
    SENTRY_LOGS_DATASET, log,
)
from app.services.gitlab_tools import build_gitlab_server
from app.services.sentry_api import discover_error_hint
from app.services.sentry_tools import (
    build_sentry_server, fmt_event_details, fmt_log_line,
)
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of, llm_cost_line

WINDOW_MIN = 45          # ± minutes around the given time
WIDE_WINDOW_MIN = 180    # retry window when the narrow one is empty
DETAIL_EVENTS = 3        # newest events fetched in full for the prompt
ANSWER_MAX = 3600

USAGE = (
    "Использование: <code>/why &lt;msisdn&gt; &lt;время&gt; &lt;описание&gt;</code>\n"
    "Время локальное: <code>14:30</code> (сегодня), <code>2026-08-03 14:30</code> "
    "или <code>03.08 14:30</code>.\n"
    "Например: <code>/why 996555123456 14:30 не смог подключить пакет</code>")

PROMPT = (
    "You are a senior backend engineer investigating a mobile subscriber's "
    "complaint for support staff.\n\n"
    "Subscriber (msisdn): {msisdn}\n"
    "Complaint: {description}\n"
    "Approximate local time: {local_time} (UTC{offset:+g}); Sentry was searched "
    "{start} .. {end} UTC (matched search key: {key}).\n\n"
    "Sentry error events of this subscriber in that window (newest first):\n"
    "{events}\n\n"
    "Application LOG lines mentioning this subscriber in that window (newest "
    "first; successful operations appear here too — a business-logic rejection "
    "often logs without producing an error event):\n{logs}\n\n"
    "Full details of the most recent event(s):\n{details}\n\n"
    "Figure out WHY the subscriber hit the problem, and decide which it is:\n"
    "- бизнес-логика: the system worked as designed (validation rejected the "
    "request, insufficient balance, product already connected, not eligible…)\n"
    "- ошибка в коде: a defect or regression in a service\n\n"
    "You have tools: read-only GitLab (read_file, find_file, search_code, "
    "blame, commit_diff, recent_commits) over the mapped service repos "
    "({repos}, ref {ref}); Sentry event_details(project, event_id) for full "
    "stacks and breadcrumbs, related_errors(trace_id) to follow a failure "
    "upstream, user_events(msisdn, …) for a wider window, and search_logs "
    "(full-text over application logs — follow a trace id or an operation "
    "name to see the whole flow). Read the code path that threw before "
    "deciding — an exception can be a deliberate business rule.\n\n"
    "Reply in Russian, plain text, no markdown:\n"
    "Вердикт: бизнес-логика | ошибка в коде | не хватает данных\n"
    "Причина: 1-3 предложения — что именно произошло и в каком сервисе\n"
    "Доказательства: какое событие/код это подтверждают\n"
    "Рекомендация: что делать (объяснение абоненту или фикс в коде)"
)


def parse_when(tokens):
    """Consume 1-2 leading tokens as a local datetime. Returns (dt, rest) or
    (None, tokens). Accepts HH:MM (today), YYYY-MM-DD HH:MM, DD.MM[.YYYY] HH:MM."""
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS)
    if not tokens:
        return None, tokens
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", tokens[0])
    if m:
        dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                         second=0, microsecond=0)
        if dt > now:                       # "23:50" said at 00:10 means yesterday
            dt -= datetime.timedelta(days=1)
        return dt, tokens[1:]
    if len(tokens) >= 2:
        for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m %H:%M"):
            try:
                dt = datetime.datetime.strptime(f"{tokens[0]} {tokens[1]}", fmt)
                if dt.year == 1900:        # DD.MM without a year
                    dt = dt.replace(year=now.year)
                return dt, tokens[2:]
            except ValueError:
                continue
    return None, tokens


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


async def _search(sentry, msisdn, center_utc):
    """(key, events, start, end) — narrow window first, then the wide one."""
    for minutes in (WINDOW_MIN, WIDE_WINDOW_MIN):
        start = _iso(center_utc - datetime.timedelta(minutes=minutes))
        end = _iso(center_utc + datetime.timedelta(minutes=minutes))
        key, events = await sentry.events_for_user(msisdn, start=start, end=end)
        if events:
            return key, events, start, end
    return None, [], start, end


async def on_why(update, ctx):
    deps = deps_of(ctx)
    args = list(ctx.args or [])
    if len(args) < 3:
        return await reply(update, USAGE)
    if not deps.sentry.enabled:
        return await reply(update, "Sentry API не настроен (SENTRY_API_TOKEN).")
    if not deps.llm.enabled:
        return await reply(update, "LLM выключен (ENABLE_LLM=false).")

    msisdn = args[0].strip()
    when_local, rest = parse_when(args[1:])
    if when_local is None or not rest:
        return await reply(update, USAGE)
    description = " ".join(rest)
    center_utc = when_local - datetime.timedelta(hours=TZ_OFFSET_HOURS)

    await reply(update, "🔎 ищу ошибки абонента и анализирую…")
    try:
        key, events, start, end = await _search(deps.sentry, msisdn, center_utc)
    except Exception as e:
        return await reply(update, f"⚠️ Sentry Discover недоступен: {esc(discover_error_hint(e))}")
    # log lines of the same window: business-logic rejections often only log,
    # so /why must work even with zero error events
    logs = []
    if SENTRY_LOGS_DATASET:
        try:
            logs = await deps.sentry.logs_for_user(msisdn, start=start, end=end,
                                                   limit=60)
        except Exception as e:
            log.warning("logs search failed msisdn=%s: %s", msisdn, e)
    if not events and not logs:
        from app.config import SENTRY_MSISDN_FIELDS
        return await reply(update,
            f"По абоненту <code>{esc(msisdn)}</code> за {esc(start)}—{esc(end)} "
            f"UTC в Sentry ничего нет — ни ошибок (искал по: "
            f"{esc(', '.join(SENTRY_MSISDN_FIELDS))}), ни строк в логах. "
            "Уточните время или проверьте номер.")

    lines, details, primary_repo = [], [], None
    for ev in events[:20]:
        slug = ev.get("project")
        lines.append(f"{(ev.get('timestamp') or '').replace('T', ' ')[:19]}  "
                     f"[{slug}]  {ev.get('title') or ev.get('message') or '?'}  "
                     f"(event {ev.get('id')})")
        if primary_repo is None:
            pid = await deps.sentry.project_id_for_slug(slug)
            primary_repo = GITLAB_PROJECTS.get(str(pid)) if pid else None
    for ev in events[:DETAIL_EVENTS]:
        try:
            full = await deps.sentry.event_details(ev.get("project"), ev.get("id"))
            details.append(fmt_event_details(full))
        except Exception as e:
            log.warning("event details failed event=%s: %s", ev.get("id"), e)

    servers, allowed = {}, []
    if ENABLE_LLM_TOOLS:
        repos = sorted(set(GITLAB_PROJECTS.values()))
        if deps.gitlab.enabled and repos:
            servers, allowed = build_gitlab_server(deps.gitlab, primary_repo or repos[0])
        s2, a2 = build_sentry_server(deps.sentry)
        if s2:
            servers.update(s2)
            allowed = list(allowed) + a2
    log_lines = [fmt_log_line(row, msg_limit=300, stack_limit=400) for row in logs]
    prompt = PROMPT.format(
        msisdn=msisdn, description=description,
        local_time=when_local.strftime("%Y-%m-%d %H:%M"), offset=TZ_OFFSET_HOURS,
        start=start, end=end, key=key or "-",
        events=("\n".join(lines) or "—"), logs=("\n".join(log_lines) or "—"),
        details=("\n---\n".join(details) or "—"),
        repos=", ".join(sorted(set(GITLAB_PROJECTS.values()))) or "нет",
        ref=GITLAB_REF)
    rec = await deps.llm.complete(prompt, mcp_servers=servers or None,
                                  allowed_tools=allowed or None)
    if rec is None or not rec.text:
        return await reply(update, "Не получилось получить ответ — смотрите логи.")
    llm_id = deps.llm_audit.put("why", rec, chat_id=update.effective_chat.id)
    await reply(update, f"🤖 {esc(rec.text)[:ANSWER_MAX]}\n{llm_cost_line(rec, llm_id)}")
