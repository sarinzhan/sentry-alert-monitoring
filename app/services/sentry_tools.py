"""Sentry tool set for the LLM — cross-service error lookup by trace id.

One tool: related_errors(trace_id). All services report to the same Sentry, so
when billing crashes because payments threw upstream, the upstream error is
usually right there as another project's event on the same trace. Each hit is
annotated with the mapped GitLab repo (via project slug -> id -> repo), so the
model knows which 'repo' value to use with the GitLab tools next.
"""
from claude_agent_sdk import tool, create_sdk_mcp_server

from app.config import GITLAB_PROJECTS, log

MAX_EVENTS = 20


def _text(s: str):
    return {"content": [{"type": "text", "text": s}]}


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
        parts = []
        for ev in events if isinstance(events, list) else []:
            slug = ev.get("project")
            pid = await sentry_api.project_id_for_slug(slug)
            repo = GITLAB_PROJECTS.get(str(pid)) if pid else None
            parts.append(
                f"{(ev.get('timestamp') or '').replace('T', ' ')[:19]}  "
                f"[{slug}]  {ev.get('title') or ev.get('message') or '?'}  "
                f"(issue {ev.get('issue') or '?'}, "
                f"repo: {repo or 'not mapped'})"
            )
        log.info("sentry tool related_errors trace=%s hits=%d", trace_id, len(parts))
        if not parts:
            return _text(f"No events found for trace {trace_id} in the last 24h — "
                         "the upstream error either wasn't reported to Sentry or "
                         "is older. Analyze from the current service's code.")
        return _text("\n".join(parts))

    server = create_sdk_mcp_server(name="sentry", version="1.0.0",
                                   tools=[related_errors])
    return {"sentry": server}, ["mcp__sentry__related_errors"]
