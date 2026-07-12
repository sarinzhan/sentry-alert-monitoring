"""AgentService — the Claude agent that answers questions and investigates errors.

Built on the Claude Agent SDK (app.services.claude_agent.run_agent) with two
in-process tool servers: Sentry (search issues/events/users) and GitLab (read
source, search code, blame, diffs). Three use-cases:

  ask(question)     free-form Q&A — /ask command and POST /ask endpoint
  investigate(ctx)  deep root-cause of one issue — /ai command
  analyze_run(...)  bounded run for the webhook-path analysis (AnalysisService)

Auth is ANTHROPIC_API_KEY or a Claude subscription OAuth token; see
app.services.claude_agent for the env plumbing (corporate proxy included).
"""
from dataclasses import dataclass

from app.config import (
    PROJECT_NAMES, GITLAB_PROJECTS,
    AGENT_MAX_TURNS, AGENT_MAX_BUDGET_USD,
    INVESTIGATE_MAX_TURNS, INVESTIGATE_MAX_BUDGET_USD,
    ANALYSIS_MAX_TURNS, ANALYSIS_MAX_BUDGET_USD, log,
)
from app.services.claude_agent import run_agent, llm_ready, money
from app.services.agent_tools import (
    build_sentry_server, build_gitlab_server, SENTRY_TOOLS, GITLAB_TOOLS,
)


def _project_map():
    """Human-readable project/repo mapping for the system prompts."""
    lines = []
    for pid, name in PROJECT_NAMES.items():
        repo = GITLAB_PROJECTS.get(pid)
        lines.append(f"  sentry project id {pid} = {name}"
                     + (f", gitlab repo: {repo}" if repo else ""))
    for pid, repo in GITLAB_PROJECTS.items():
        if pid not in PROJECT_NAMES:
            lines.append(f"  sentry project id {pid}: gitlab repo {repo}")
    return "\n".join(lines) or "  (no static mapping configured)"


ASK_SYSTEM_PROMPT = (
    "You are a Sentry monitoring assistant for a backend team. Answer questions "
    "about errors, users and code by calling the tools — never invent issue ids, "
    "counts, titles or file contents.\n"
    "- Resolve a human project name to its slug with list_projects when unsure.\n"
    "- Prefer search_issues with a Sentry query string (e.g. 'is:unresolved "
    "level:error'), an environment, and a stats_period like '24h'.\n"
    "- For questions about a specific user ('what errors does user 12345 get?') "
    "use search_user_issues.\n"
    "- For questions about a user by msisdn or about a single request, use "
    "search_events (e.g. 'msisdn:996555123456 \"error text\"'); follow the "
    "returned trace id with 'trace:<id>' to see the whole call chain across "
    "services, and get_event for full detail (breadcrumbs, tags) of one event.\n"
    "- For questions about code paths ('why does GET /api/orders return 500?') "
    "find the issue first, then read the actual source with the gitlab tools: "
    "issue_latest_event gives class/file names, find_file maps them to repo "
    "paths, read_file shows the code.\n"
    "- Given only a user-facing error text and no known repo, use search_code "
    "WITHOUT the repo argument to sweep every repo for that string.\n"
    "Known projects:\n{projects}\n"
    "Answer in the language of the question, concise and factual; cite issue "
    "titles, counts and permalinks. If the tools return nothing, say so plainly."
)

INVESTIGATE_SYSTEM_PROMPT = (
    "You are a senior backend engineer doing root-cause analysis of ONE Sentry "
    "issue. Work strictly from evidence:\n"
    "1. Pull the full event: get_event with the project slug and event id from "
    "the message (fall back to issue_latest_event if there is no event id). It "
    "gives the stacktrace, breadcrumbs (the service's last actions before the "
    "crash), tags and the trace id.\n"
    "2. If a trace id exists, search_events with 'trace:<id>' — every event of "
    "that request across all services, time-ordered. Reconstruct what was "
    "called and find where the chain broke.\n"
    "3. If you still need context, search_events with 'msisdn:<x>' and "
    "end=<event time> shows the user's actions leading up to the error.\n"
    "4. Map the crashing frames to repo paths with find_file (pass the frame's "
    "module and filename), then read_file at the crash lines.\n"
    "5. Check blame_line for the crash line and commit_diff of that commit — did "
    "a recent change introduce this? recent_commits on the file helps when the "
    "line is ambiguous.\n"
    "6. Conclude.\n"
    "Known projects:\n{projects}\n"
    "Output PLAIN TEXT (no markdown), in the language of the report:\n"
    "Root cause: <1-2 sentences with file:line evidence>\n"
    "Fix: <1-3 concrete sentences>\n"
    "If a specific commit caused it, name the sha and author.\n"
    "Do not speculate beyond what the tools showed you."
)

ANALYSIS_SYSTEM_PROMPT = (
    "You are a senior backend engineer triaging a Sentry error. The stack trace, "
    "current source around the crash site and the most recent diff are already "
    "in the message — use tools only if that context is insufficient. Use the "
    "recent change diff to judge whether it introduced the bug.\n"
    "Reply in at most 4 short lines, plain text:\n"
    "Likely cause: <one sentence>\n"
    "Suggested fix: <one or two sentences>"
)


@dataclass
class AgentAnswer:
    """Result of an agent run: the text plus token/cost accounting."""
    text: str
    in_tokens: int
    out_tokens: int
    cost: float
    cost_estimated: bool = False

    @property
    def cost_line(self):
        tail = " (est)" if self.cost_estimated else ""
        return f"💰 {money(self.cost)}{tail} · {self.in_tokens} in / {self.out_tokens} out"


class AgentService:
    def __init__(self, sentry_query, gitlab):
        self._sentry = sentry_query      # SentryQueryClient
        self._gitlab = gitlab            # GitLabClient
        self._servers = {"sentry": build_sentry_server(sentry_query)}
        self._allowed = list(SENTRY_TOOLS)
        if gitlab.enabled:
            self._servers["gitlab"] = build_gitlab_server(gitlab)
            self._allowed += GITLAB_TOOLS
        projects = _project_map()
        self._ask_prompt = ASK_SYSTEM_PROMPT.format(projects=projects)
        self._investigate_prompt = INVESTIGATE_SYSTEM_PROMPT.format(projects=projects)

    @property
    def enabled(self):
        return llm_ready()

    async def _run(self, prompt, system_prompt, max_turns, max_budget_usd):
        res = await run_agent(
            prompt,
            system_prompt=system_prompt,
            mcp_servers=self._servers,
            allowed_tools=self._allowed,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
        )
        if res is None:
            return None
        if not res.ok:
            log.warning("agent run not ok: subtype=%s", res.subtype)
            if res.subtype in ("error_max_turns", "error_max_budget_usd"):
                return AgentAnswer(
                    text="Не уложился в лимит шагов/бюджета — попробуйте сузить вопрос.",
                    in_tokens=res.in_tokens, out_tokens=res.out_tokens,
                    cost=res.cost, cost_estimated=res.cost_estimated)
            return None
        return AgentAnswer(text=res.text, in_tokens=res.in_tokens,
                           out_tokens=res.out_tokens, cost=res.cost,
                           cost_estimated=res.cost_estimated)

    async def ask(self, question: str):
        """Free-form question -> AgentAnswer, or None (disabled/failed)."""
        if not self.enabled or not question.strip():
            return None
        return await self._run(question, self._ask_prompt,
                               AGENT_MAX_TURNS, AGENT_MAX_BUDGET_USD)

    async def investigate(self, ctx: dict):
        """Deep root-cause of one issue from its stored parsed context (/ai)."""
        if not self.enabled:
            return None
        chain = "\n".join(f"{t}: {v}" for t, v in (ctx.get("exc_chain") or [])) \
            or f"{ctx.get('type')}: {ctx.get('value')}"
        stack = "\n".join(ctx.get("frames_full") or [])
        lines = [
            "Investigate this Sentry issue.",
            f"Issue id: {ctx.get('issue_id')}",
            f"Project: {ctx.get('project')} (sentry project id {ctx.get('project_id')})",
            f"Title: {ctx.get('title')}",
            f"Culprit: {ctx.get('culprit')}",
            f"Environment: {ctx.get('environment')}",
        ]
        # investigation anchors — present only for alerts parsed after they
        # were added to the webhook parser
        for label, key in (("Event id", "event_id"), ("Event time", "timestamp"),
                           ("Trace id", "trace_id"), ("msisdn", "msisdn")):
            if ctx.get(key):
                lines.append(f"{label}: {ctx[key]}")
        tags = {k: v for k, v in (ctx.get("tags") or {}).items()
                if k not in ("msisdn", "environment", "level")}
        if tags:
            lines.append("Tags: " + ", ".join(f"{k}={v}" for k, v in tags.items()))
        prompt = (
            "\n".join(lines)
            + f"\nException chain (most recent last):\n{chain}\n\n"
            f"Stack (crash site first):\n{stack}\n\n"
            f"Verify against the real event data and read the real code before concluding."
        )
        return await self._run(prompt, self._investigate_prompt,
                               INVESTIGATE_MAX_TURNS, INVESTIGATE_MAX_BUDGET_USD)

    async def analyze_run(self, prompt: str):
        """Bounded run for the webhook-path cause/fix analysis (AnalysisService).
        Context is prefetched into the prompt, so this normally finishes in 1-2
        turns; tools are available as a fallback."""
        if not self.enabled:
            return None
        return await self._run(prompt, ANALYSIS_SYSTEM_PROMPT,
                               ANALYSIS_MAX_TURNS, ANALYSIS_MAX_BUDGET_USD)

    async def aclose(self):
        # keeps the existing ownership contract: closing the agent closes the
        # SentryQueryClient it wraps (GitLabClient is closed by the controller).
        await self._sentry.aclose()
