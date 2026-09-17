"""Web investigation form backend: search Sentry by any of request_id /
device_id / msisdn over a chosen period.

The web analog of /req + /why (first iteration: search only, no LLM verdict).
Every provided identifier is searched (each over its configured key list),
events are merged and deduplicated; for an msisdn the application log lines of
the same window are fetched too — a business-logic rejection often only logs.

The period is either a preset (1h/24h/3d/7d/14d/30d — Sentry statsPeriod,
default 3d) or a custom LOCAL date range (dates only, inclusive), converted
to UTC via TZ_OFFSET_HOURS.
"""
import datetime

from app.config import (
    SENTRY_MSISDN_FIELDS, SENTRY_REQUEST_ID_FIELDS, SENTRY_DEVICE_ID_FIELDS,
    SENTRY_LOGS_DATASET, TZ_OFFSET_HOURS,
    GITLAB_PROJECTS, GITLAB_REF, ENABLE_LLM_TOOLS, AGENT_MAX_TURNS, log,
)

PERIODS = ("1h", "24h", "3d", "7d", "14d", "30d")
DEFAULT_PERIOD = "3d"
EVENT_LIMIT = 100
LOG_LIMIT = 60

# msisdn error-event search: the configured attribute keys first; when none of
# them hit, full-text over the event message — some services only mention the
# msisdn inside the text (find_events tries keys in order until one returns hits;
# the log search is full-text already via SENTRY_LOGS_MSISDN_QUERY)
MSISDN_KEYS = list(SENTRY_MSISDN_FIELDS) + ["message"]


class ValidationError(ValueError):
    pass


def _parse_date(value, name):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValidationError(f"не понял дату «{name}»: {value!r}")


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def build_window(period=None, date_from=None, date_to=None):
    """One Discover time range: {'stats_period': …} for a preset, or
    {'start': …, 'end': …} (UTC ISO) for a custom local date range."""
    date_from = (date_from or "").strip()
    date_to = (date_to or "").strip()
    if date_from or date_to:
        if not (date_from and date_to):
            raise ValidationError("укажите обе даты — «с» и «по»")
        start_local = _parse_date(date_from, "с")
        end_local = _parse_date(date_to, "по") + datetime.timedelta(days=1)
        if start_local >= end_local:
            raise ValidationError("дата «с» позже даты «по»")
        offset = datetime.timedelta(hours=TZ_OFFSET_HOURS)
        return {"start": _iso(start_local - offset),
                "end": _iso(end_local - offset)}
    period = (period or "").strip() or DEFAULT_PERIOD
    if period not in PERIODS:
        raise ValidationError(
            f"период {period!r} не поддерживается ({', '.join(PERIODS)})")
    return {"stats_period": period}


async def investigate(sentry, *, description, request_id=None, device_id=None,
                      msisdn=None, period=None, date_from=None, date_to=None,
                      environment=None):
    """Run the search; returns a JSON-ready dict. Raises ValidationError on
    bad input, propagates Sentry API errors to the caller."""
    description = (description or "").strip()
    ids = [("request_id", SENTRY_REQUEST_ID_FIELDS, (request_id or "").strip()),
           ("device_id", SENTRY_DEVICE_ID_FIELDS, (device_id or "").strip()),
           ("msisdn", MSISDN_KEYS, (msisdn or "").strip())]
    if not description:
        raise ValidationError("описание обязательно")
    if not any(v for _, _, v in ids):
        raise ValidationError(
            "укажите хотя бы одно из: request id, device id, номер (msisdn)")
    window = build_window(period, date_from, date_to)
    envs = [environment.strip()] if (environment or "").strip() else None

    searches, events_by_id, logs = [], {}, []
    for id_type, keys, value in ids:
        if not value:
            continue
        key, events = await sentry.find_events(
            keys, value, limit=EVENT_LIMIT, environments=envs, **window)
        searches.append({"id_type": id_type, "value": value,
                         "matched_field": key, "count": len(events)})
        for ev in events:
            events_by_id.setdefault(str(ev.get("id")), ev)
    m = next((v for t, _, v in ids if t == "msisdn" and v), None)
    if m and SENTRY_LOGS_DATASET:
        try:
            logs = await sentry.logs_for_user(m, limit=LOG_LIMIT,
                                              environments=envs, **window)
        except Exception as e:
            log.warning("web logs search failed msisdn=%s: %s", m, e)

    events = sorted(events_by_id.values(),
                    key=lambda ev: ev.get("timestamp") or "", reverse=True)
    return {
        "description": description,
        "environment": (envs or [None])[0],
        "window": window,
        "tz_offset_hours": TZ_OFFSET_HOURS,
        "searches": searches,
        "count": len(events),
        "events": events,
        "logs": logs,
    }


# --- LLM explanation for support staff (the /why analog of the web form) ---

DETAIL_EVENTS = 3        # newest events fetched in full for the prompt

EXPLAIN_PROMPT = (
    "You are a senior backend engineer helping FIRST-LINE TECH SUPPORT of a "
    "mobile operator. The support agent is NOT a programmer.\n\n"
    "Complaint: {description}\n"
    "Identifiers: {idents}\n"
    "Search window: {window}; environment: {env}.\n\n"
    "Sentry error events found (newest first):\n{events}\n\n"
    "Application LOG lines of the same window (newest first; successful "
    "operations appear here too — a business-logic rejection often logs "
    "without producing an error event):\n{logs}\n\n"
    "Full details of the newest event(s):\n{details}\n\n"
    "{tools}"
    "Figure out WHY the user hit the problem. An exception can be a "
    "deliberate business rule — check before calling it a bug.\n\n"
    "Reply in SIMPLE RUSSIAN a non-programmer understands: no stack traces, "
    "no HTTP codes, no jargon (расшифруй, если без термина никак). Plain "
    "text, no markdown, exactly this structure:\n"
    "Что произошло: 1-2 предложения простыми словами\n"
    "Причина: почему это происходит — бизнес-правило, ошибка сервиса или "
    "проблема данных; если точно не ясно, самая вероятная версия с пометкой "
    "«предположительно»\n"
    "Что сказать клиенту: готовая вежливая формулировка\n"
    "Что дальше: может ли поддержка решить сама (и как), или передать "
    "разработчикам — какой команде/сервису и с какой информацией"
)

TOOLS_NOTE = (
    "You have tools: read-only GitLab (read_file, find_file, search_code, "
    "blame, commit_diff, recent_commits) over the mapped service repos "
    "({repos}, ref {ref}); Sentry event_details(project, event_id), "
    "related_errors(trace_id), user_events(msisdn, …) and search_logs "
    "(full-text over application logs). Read the code path that threw "
    "before deciding.\n"
    "IMPORTANT: you have a hard budget of {turns} turns and MUST deliver the "
    "final answer within it. Spend at most half the budget on tool calls; if "
    "a tool errors twice (e.g. GitLab auth), stop using it. When the budget "
    "runs low, stop investigating and answer with the best conclusion from "
    "what you already have — a partial answer beats no answer.\n\n"
)


async def explain(sentry, gitlab, llm, *, search, on_event=None):
    """LLM explanation of a finished investigate() result, written for
    support staff. Returns the LlmCall record, or None on LLM failure.
    on_event streams the model's live progress (see LlmClient.complete)."""
    from app.services.gitlab_tools import build_gitlab_server
    from app.services.sentry_tools import (
        build_sentry_server, fmt_event_details, fmt_log_line,
    )

    events, logs = search["events"], search["logs"]
    lines, details, primary_repo = [], [], None
    for ev in events[:20]:
        slug = ev.get("project")
        lines.append(f"{(ev.get('timestamp') or '').replace('T', ' ')[:19]}  "
                     f"[{slug}]  {ev.get('title') or ev.get('message') or '?'}  "
                     f"(event {ev.get('id')})")
        if primary_repo is None:
            pid = await sentry.project_id_for_slug(slug)
            primary_repo = GITLAB_PROJECTS.get(str(pid)) if pid else None
    for ev in events[:DETAIL_EVENTS]:
        try:
            full = await sentry.event_details(ev.get("project"), ev.get("id"))
            details.append(fmt_event_details(full))
        except Exception as e:
            log.warning("web explain: event details failed event=%s: %s",
                        ev.get("id"), e)

    servers, allowed = {}, []
    if ENABLE_LLM_TOOLS:
        repos = sorted(set(GITLAB_PROJECTS.values()))
        if gitlab.enabled and repos:
            servers, allowed = build_gitlab_server(gitlab, primary_repo or repos[0])
        s2, a2 = build_sentry_server(sentry)
        if s2:
            servers.update(s2)
            allowed = list(allowed) + a2

    w = search["window"]
    window_h = (f"last {w['stats_period']}" if "stats_period" in w
                else f"{w['start']} .. {w['end']} UTC")
    idents = "; ".join(f"{s['id_type']}={s['value']}"
                       + (f" (matched field {s['matched_field']})" if s["matched_field"] else "")
                       for s in search["searches"])
    tools_note = TOOLS_NOTE.format(
        repos=", ".join(sorted(set(GITLAB_PROJECTS.values()))) or "нет",
        ref=GITLAB_REF, turns=AGENT_MAX_TURNS) if servers else ""
    log_lines = [fmt_log_line(row, msg_limit=300, stack_limit=400) for row in logs]
    prompt = EXPLAIN_PROMPT.format(
        description=search["description"], idents=idents, window=window_h,
        env=search["environment"] or "all",
        events=("\n".join(lines) or "—"), logs=("\n".join(log_lines) or "—"),
        details=("\n---\n".join(details) or "—"), tools=tools_note)
    return await llm.complete(prompt, mcp_servers=servers or None,
                              allowed_tools=allowed or None, on_event=on_event)
