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
# Agent-first: the model receives ONLY the complaint, the identifiers and the
# chosen window — it decides itself what to search, in what order, and when
# to stop (find_events / search_logs / event_details / related_errors +
# read-only GitLab).

EXPLAIN_PROMPT = (
    "You are a senior backend engineer helping FIRST-LINE TECH SUPPORT of a "
    "mobile operator. The support agent is NOT a programmer.\n\n"
    "Complaint: {description}\n"
    "Identifiers: {idents}\n"
    "Time window the agent chose: {window} — use it in tool calls; widen it "
    "if you find nothing. Environment: {env}.\n\n"
    "Investigate the complaint YOURSELF with the tools — you decide what to "
    "look at and in what order:\n"
    "- find_events(kind, value, …): error events by request_id / device_id / "
    "msisdn across all services\n"
    "- search_logs(query, …): full-text over application LOG lines (INFO "
    "too — a business-logic rejection often only logs, without an error "
    "event)\n"
    "- event_details(project, event_id): full stack, tags, breadcrumbs\n"
    "- related_errors(trace_id): follow a failure upstream across services\n"
    "- user_events(msisdn, …): everything else that happened to the user\n"
    "- read-only GitLab over the mapped repos ({repos}, ref {ref}): "
    "read_file, find_file, search_code, blame, commit_diff, recent_commits — "
    "read the code path that threw before deciding; an exception can be a "
    "deliberate business rule\n\n"
    "Budget: a hard limit of {turns} turns — deliver the final answer within "
    "it. Spend at most half on tool calls; if a tool errors twice, stop "
    "using it; when the budget runs low, answer with the best conclusion "
    "from what you have. If you find nothing at all, say so honestly and "
    "suggest what the support agent should clarify (exact time, other "
    "identifiers).\n\n"
    "Reply in SIMPLE RUSSIAN a non-programmer understands: no stack traces, "
    "no HTTP codes, no jargon (расшифруй, если без термина никак). Plain "
    "text, no markdown, exactly this structure:\n"
    "Что произошло: 1-2 предложения простыми словами\n"
    "Причина: почему это происходит — бизнес-правило, ошибка сервиса или "
    "проблема данных; если точно не ясно, самая вероятная версия с пометкой "
    "«предположительно»\n"
    "Доказательства: на чём основан вывод — конкретные строки логов и "
    "события (время, сервис, дословная цитата сообщения), которые ты нашёл; "
    "это единственный раздел, где технический текст уместен\n"
    "Что сказать клиенту: готовая вежливая формулировка\n"
    "Что дальше: может ли поддержка решить сама (и как), или передать "
    "разработчикам — какой команде/сервису и с какой информацией"
)


async def explain(sentry, gitlab, llm, *, description, request_id=None,
                  device_id=None, msisdn=None, period=None, date_from=None,
                  date_to=None, environment=None, on_event=None):
    """Agentic LLM investigation of a complaint, written for support staff.
    Returns the LlmCall record, or None on LLM failure. Raises
    ValidationError on bad input. on_event streams the model's live progress
    (see LlmClient.complete)."""
    from app.services.gitlab_tools import build_gitlab_server
    from app.services.sentry_tools import build_sentry_server

    description = (description or "").strip()
    idents = [(kind, (value or "").strip())
              for kind, value in (("request_id", request_id),
                                  ("device_id", device_id),
                                  ("msisdn", msisdn)) if (value or "").strip()]
    if not description:
        raise ValidationError("описание обязательно")
    if not idents:
        raise ValidationError(
            "укажите хотя бы одно из: request id, device id, номер (msisdn)")
    window = build_window(period, date_from, date_to)

    servers, allowed = {}, []
    if ENABLE_LLM_TOOLS:
        repos = sorted(set(GITLAB_PROJECTS.values()))
        if gitlab.enabled and repos:
            servers, allowed = build_gitlab_server(gitlab, repos[0])
        s2, a2 = build_sentry_server(sentry)
        if s2:
            servers.update(s2)
            allowed = list(allowed) + a2
    if not servers:
        raise ValidationError(
            "анализ недоступен: инструменты LLM выключены (ENABLE_LLM_TOOLS) "
            "или Sentry API не настроен")

    window_h = (f"last {window['stats_period']}" if "stats_period" in window
                else f"{window['start']} .. {window['end']} UTC")
    prompt = EXPLAIN_PROMPT.format(
        description=description,
        idents="; ".join(f"{k}={v}" for k, v in idents),
        window=window_h,
        env=(environment or "").strip() or "all",
        repos=", ".join(sorted(set(GITLAB_PROJECTS.values()))) or "нет",
        ref=GITLAB_REF, turns=AGENT_MAX_TURNS)
    return await llm.complete(prompt, mcp_servers=servers,
                              allowed_tools=allowed, on_event=on_event)
