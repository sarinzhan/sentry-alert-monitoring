"""Sentry tool set for the LLM — cross-service lookups.

Three tools: related_errors(trace_id), user_events(msisdn, ...) and
event_details(project, event_id). All services report to the same Sentry, so
when billing crashes because payments threw upstream, the upstream error is
usually right there as another project's event on the same trace. Each hit is
annotated with the mapped GitLab repo (via project slug -> id -> repo), so the
model knows which 'repo' value to use with the GitLab tools next.
"""
from claude_agent_sdk import tool, create_sdk_mcp_server

from app.config import (
    GITLAB_PROJECTS, SENTRY_LOGS_DATASET,
    SENTRY_MSISDN_FIELDS, SENTRY_REQUEST_ID_FIELDS, SENTRY_DEVICE_ID_FIELDS, log,
)
from app.services.sentry_api import LOG_FIELDS

MAX_EVENTS = 20
MAX_LOG_LINES = 40
MAX_LOG_MSG = 250
MAX_FRAMES = 12
MAX_CRUMBS = 20
MAX_DETAILS = 5000


def _text(s: str):
    return {"content": [{"type": "text", "text": s}]}


# fields fmt_log_line already renders — anything else requested shows as key=value
_LOG_CORE = {"timestamp", "message", "resource.service.name",
             "exception.type", "exception.message", "exception.stacktrace"}


def fmt_log_line(row, msg_limit=250, stack_limit=0, extra_fields=()):
    """One log row as text: time [service] message, plus exception type/message
    when present, optionally the (truncated) stacktrace and extra attributes.
    Shared by the search_logs tool and the /activity, /why prompt builders."""
    msg = " ".join(str(row.get("message") or "").split())[:msg_limit]
    line = (f"{(row.get('timestamp') or '').replace('T', ' ')[:19]}  "
            f"[{row.get('resource.service.name') or '?'}]  {msg}")
    exc_t, exc_m = row.get("exception.type"), row.get("exception.message")
    if exc_t or exc_m:
        em = " ".join(str(exc_m or "").split())[:msg_limit]
        line += f"\n    exception: {exc_t or '?'}" + (f": {em}" if em else "")
    if stack_limit:
        stack = str(row.get("exception.stacktrace") or "").strip()
        if stack:
            line += f"\n    stack: {stack[:stack_limit]}"
    extras = [f"{f}={row.get(f)}" for f in extra_fields
              if f not in _LOG_CORE and row.get(f) not in (None, "")]
    if extras:
        line += "\n    " + "  ".join(extras)
    return line


async def _fmt_hits(sentry_api, events):
    """One line per Discover hit, annotated with the mapped GitLab repo and the
    event id (for event_details)."""
    parts = []
    for ev in events if isinstance(events, list) else []:
        slug = ev.get("project")
        pid = await sentry_api.project_id_for_slug(slug)
        repo = GITLAB_PROJECTS.get(str(pid)) if pid else None
        parts.append(
            f"{(ev.get('timestamp') or '').replace('T', ' ')[:19]}  "
            f"[{slug}]  {ev.get('title') or ev.get('message') or '?'}  "
            f"(event {ev.get('id') or '?'}, issue {ev.get('issue') or '?'}, "
            f"repo: {repo or 'not mapped'})"
        )
    return parts


def fmt_event_details(ev):
    """Compact text view of one full event: exception chain + top frames,
    request, tags, breadcrumbs. Shared by the tool and the /why, /activity
    prompt builders."""
    parts = [f"title: {ev.get('title')}", f"time: {ev.get('dateCreated')}"]
    tags = ev.get("tags") or []
    if tags:
        parts.append("tags: " + ", ".join(
            f"{t.get('key')}={t.get('value')}" for t in tags))
    for entry in ev.get("entries") or []:
        etype = entry.get("type")
        data = entry.get("data") or {}
        if etype == "exception":
            for exc in data.get("values") or []:
                parts.append(f"exception: {exc.get('type')}: {exc.get('value')}")
                frames = (exc.get("stacktrace") or {}).get("frames") or []
                for f in frames[-MAX_FRAMES:]:      # crash site is last
                    where = f.get("module") or f.get("filename") or "?"
                    parts.append(f"  at {where}:{f.get('lineNo')} "
                                 f"in {f.get('function')}"
                                 + (" [in-app]" if f.get("inApp") else ""))
        elif etype == "breadcrumbs":
            crumbs = (data.get("values") or [])[-MAX_CRUMBS:]
            if crumbs:
                parts.append("breadcrumbs (oldest first):")
                for c in crumbs:
                    msg = c.get("message") or c.get("data") or ""
                    parts.append(f"  {str(c.get('timestamp') or '')[:19]} "
                                 f"[{c.get('category')}] {msg}")
        elif etype == "request":
            parts.append(f"request: {data.get('method')} {data.get('url')}")
    return "\n".join(str(p) for p in parts)[:MAX_DETAILS]


def build_sentry_server(sentry_api):
    """(mcp_servers dict, allowed_tools list), or (None, None) without an API token."""
    if not sentry_api.enabled:
        return None, None

    @tool(
        "related_errors",
        "Error events across ALL services that handled one request, by "
        "distributed-tracing id (last 24h). Call this when the failure looks "
        "caused by an upstream service — e.g. an HTTP 5xx or timeout from a "
        "downstream call — to find the error that actually started it. Each "
        "hit shows which GitLab repo to read next.",
        {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string",
                             "description": "The trace id from the event context"},
            },
            "required": ["trace_id"],
        },
    )
    async def related_errors(args):
        trace_id = (args.get("trace_id") or "").strip()
        try:
            events = await sentry_api.events_for_trace(trace_id, limit=MAX_EVENTS)
        except Exception as e:
            return _text(f"Error querying Sentry for trace {trace_id}: {e}")
        parts = await _fmt_hits(sentry_api, events)
        log.info("sentry tool related_errors trace=%s hits=%d", trace_id, len(parts))
        if not parts:
            return _text(f"No events found for trace {trace_id} in the last 24h — "
                         "the upstream error either wasn't reported to Sentry or "
                         "is older. Analyze from the current service's code.")
        return _text("\n".join(parts))

    @tool(
        "find_events",
        "Error events matching ONE identifier across all services. kind is "
        "'request_id', 'device_id' or 'msisdn' — the configured Sentry search "
        "keys for that kind are tried in order, then full-text over the event "
        "message (some services only mention identifiers in the text). Give "
        "start+end (ISO 8601 UTC) or a period like '1h'/'3d'. Each hit shows "
        "the event id for event_details and the GitLab repo to read next.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": ["request_id", "device_id", "msisdn"],
                         "description": "Which identifier this is"},
                "value": {"type": "string", "description": "The identifier value"},
                "start": {"type": "string",
                          "description": "Window start, ISO 8601 UTC (with end)"},
                "end": {"type": "string",
                        "description": "Window end, ISO 8601 UTC (with start)"},
                "period": {"type": "string",
                           "description": "Alternative to start/end: '1h'/'24h'/'3d'"},
            },
            "required": ["kind", "value"],
        },
    )
    async def find_events(args):
        kind = (args.get("kind") or "").strip()
        value = (args.get("value") or "").strip()
        keys = {"request_id": SENTRY_REQUEST_ID_FIELDS,
                "device_id": SENTRY_DEVICE_ID_FIELDS,
                "msisdn": SENTRY_MSISDN_FIELDS}.get(kind)
        if not keys or not value:
            return _text("kind must be request_id|device_id|msisdn and value non-empty")
        start, end = args.get("start"), args.get("end")
        try:
            key, events = await sentry_api.find_events(
                list(keys) + ["message"], value,
                start=start if (start and end) else None,
                end=end if (start and end) else None,
                stats_period=None if (start and end) else (args.get("period") or "3d"),
                limit=MAX_EVENTS)
        except Exception as e:
            return _text(f"Error querying Sentry for {kind} {value}: {e}")
        parts = await _fmt_hits(sentry_api, events)
        log.info("sentry tool find_events kind=%s value=%s hits=%d",
                 kind, value, len(parts))
        if not parts:
            return _text(f"No error events for {kind}={value} in that window "
                         f"(tried keys: {', '.join(list(keys) + ['message'])}). "
                         "Try a wider window or search_logs.")
        return _text(f"matched search key: {key}\n" + "\n".join(parts))

    @tool(
        "user_events",
        "Error events of ONE user (msisdn) across all services. Give either a "
        "start+end window (ISO 8601 UTC) or a period like '3h'/'24h'. Use this "
        "to see what else happened to the user around the failure. Each hit "
        "shows the event id for event_details.",
        {
            "type": "object",
            "properties": {
                "msisdn": {"type": "string", "description": "The user's msisdn"},
                "start": {"type": "string",
                          "description": "Window start, ISO 8601 UTC (with end)"},
                "end": {"type": "string",
                        "description": "Window end, ISO 8601 UTC (with start)"},
                "period": {"type": "string",
                           "description": "Alternative to start/end: '1h'/'3h'/'24h'"},
            },
            "required": ["msisdn"],
        },
    )
    async def user_events(args):
        msisdn = (args.get("msisdn") or "").strip()
        start, end = args.get("start"), args.get("end")
        try:
            _, events = await sentry_api.events_for_user(
                msisdn,
                start=start if (start and end) else None,
                end=end if (start and end) else None,
                stats_period=None if (start and end) else (args.get("period") or "24h"),
                limit=MAX_EVENTS)
        except Exception as e:
            return _text(f"Error querying Sentry for user {msisdn}: {e}")
        parts = await _fmt_hits(sentry_api, events)
        log.info("sentry tool user_events msisdn=%s hits=%d", msisdn, len(parts))
        if not parts:
            return _text(f"No error events for user {msisdn} in that window.")
        return _text("\n".join(parts))

    @tool(
        "event_details",
        "Full detail of one Sentry event: exception chain with stack frames, "
        "request URL, tags, breadcrumbs (the user's preceding actions). Call "
        "this on event ids from user_events/related_errors to see what "
        "actually happened.",
        {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "Project slug from the hit line"},
                "event_id": {"type": "string", "description": "The event id"},
            },
            "required": ["project", "event_id"],
        },
    )
    async def event_details(args):
        slug = (args.get("project") or "").strip()
        event_id = (args.get("event_id") or "").strip()
        try:
            ev = await sentry_api.event_details(slug, event_id)
        except Exception as e:
            return _text(f"Error fetching event {event_id} from {slug}: {e}")
        log.info("sentry tool event_details project=%s event=%s", slug, event_id)
        return _text(fmt_event_details(ev))

    @tool(
        "search_logs",
        "Full-text search over application LOG lines from all services (not "
        "just errors — INFO logs of successful operations too). Identifiers "
        "like the msisdn usually appear INSIDE the message text, so query "
        'e.g. message:"996555123456", or add words from the operation. Each '
        "hit shows the message plus exception.type/message/stacktrace when "
        "present; pass 'fields' to also fetch other attributes (list them "
        "with log_fields). Use this to reconstruct what a user or service "
        "actually did around a failure. Give start+end (ISO 8601 UTC) or a "
        "period like '1h'/'24h'.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": 'Search query, e.g. message:"996555123456"'},
                "start": {"type": "string",
                          "description": "Window start, ISO 8601 UTC (with end)"},
                "end": {"type": "string",
                        "description": "Window end, ISO 8601 UTC (with start)"},
                "period": {"type": "string",
                           "description": "Alternative to start/end: '1h'/'24h'"},
                "fields": {"type": "array", "items": {"type": "string"},
                           "description": "Extra log attributes to fetch "
                                          "(keys from log_fields)"},
            },
            "required": ["query"],
        },
    )
    async def search_logs(args):
        q = (args.get("query") or "").strip()
        start, end = args.get("start"), args.get("end")
        extra = [f for f in (args.get("fields") or []) if isinstance(f, str)]
        try:
            rows = await sentry_api.search_logs(
                q,
                start=start if (start and end) else None,
                end=end if (start and end) else None,
                stats_period=None if (start and end) else (args.get("period") or "24h"),
                limit=MAX_LOG_LINES,
                fields=(list(LOG_FIELDS) + extra) if extra else None)
        except Exception as e:
            return _text(f"Error searching logs for '{q}': {e}")
        parts = [fmt_log_line(row, MAX_LOG_MSG, stack_limit=500, extra_fields=extra)
                 for row in (rows if isinstance(rows, list) else [])]
        log.info("sentry tool search_logs q=%s hits=%d", q[:80], len(parts))
        if not parts:
            return _text(f"No log lines match '{q}' in that window.")
        return _text("\n".join(parts))

    @tool(
        "log_fields",
        "List every log attribute key that exists in this Sentry org (fetched "
        "from the API). Call this before asking search_logs for extra 'fields' "
        "so you only request attributes that are really there.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def log_fields(args):
        try:
            attrs = await sentry_api.log_attributes()
        except Exception as e:
            return _text(f"Error listing log attributes: {e}")
        if not attrs:
            return _text("Attribute listing is not supported by this Sentry "
                         "version. Known-good fields: " + ", ".join(LOG_FIELDS))
        return _text("\n".join(attrs))

    tools = [find_events, related_errors, user_events, event_details]
    if SENTRY_LOGS_DATASET:
        tools += [search_logs, log_fields]
    server = create_sdk_mcp_server(name="sentry", version="1.0.0", tools=tools)
    return {"sentry": server}, [f"mcp__sentry__{t.name}" for t in tools]
