"""report_gap tool — the LLM's channel for «what I'm missing».

A single tool attached to the agentic flows (alert analysis, /ai, incident
verification, web explain/ask) next to the GitLab/Sentry/notes servers. The
model calls it when the data it needs to reason isn't there: no logs in Sentry,
too few logs, no source access, missing context. Rows show up in the web panel
«Что нужно LLM» so a human knows what to add (more logging, a GitLab repo
mapping, etc.). Callers append GAPS_PROMPT so the model knows to use it.
"""
from claude_agent_sdk import tool, create_sdk_mcp_server

from app.config import log
from app.repositories.gaps import KINDS

GAPS_PROMPT = (
    "\n\nIf you cannot fully investigate because the DATA you need is missing — "
    "there are no logs in Sentry for this service, too few log lines to reason, "
    "you have no source access (the repo isn't mapped), or key context is absent "
    "— call report_gap to flag it for the team (kinds: "
    + ", ".join(KINDS) + "). Report the gap for the specific service/project it "
    "concerns. This does not replace your answer — still answer as best you can."
)


def _text(s: str):
    return {"content": [{"type": "text", "text": s}]}


def build_observability_server(gaps, project_hint=None, issue_id=None):
    """(mcp_servers dict, allowed_tools list). gaps is the shared GapsRepo;
    project_hint pre-fills the project for single-service flows (alert /ai /
    verify); issue_id tags the row with the triggering issue when known."""

    @tool(
        "report_gap",
        "Flag that you are MISSING data needed to investigate properly, so a "
        "human can add it. Use when: no logs exist in Sentry for a service, there "
        "are too few logs to reason, you have no source access (repo not mapped), "
        "or key context is absent. Kinds: " + ", ".join(KINDS) + ".",
        {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "The service/project the gap is about "
                                           "(name or id as you know it)."},
                "kind": {"type": "string", "enum": list(KINDS),
                         "description": "What is missing."},
                "detail": {"type": "string",
                           "description": "One sentence: what you needed and "
                                          "couldn't find (e.g. 'no INFO logs for "
                                          "the top-up flow in the last hour')."},
            },
            "required": ["kind", "detail"],
        },
    )
    async def report_gap(args):
        kind = (args.get("kind") or "other").strip()
        detail = (args.get("detail") or "").strip()
        project = (args.get("project") or project_hint or "").strip() or None
        if not detail:
            return _text("detail must be non-empty")
        try:
            gaps.report(project, kind, detail, issue_id=issue_id)
        except Exception as e:
            return _text(f"Error recording gap: {e}")
        log.info("llm gap reported project=%s kind=%s issue=%s", project, kind, issue_id)
        return _text(f"Noted the gap ({kind}) for {project or 'unknown project'}. "
                     "Continue with your best answer.")

    server = create_sdk_mcp_server(name="observability", version="1.0.0",
                                   tools=[report_gap])
    return {"observability": server}, ["mcp__observability__report_gap"]
