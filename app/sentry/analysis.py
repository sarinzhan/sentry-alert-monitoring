"""AnalysisService — the LLM cause/fix use-case.

Orchestrates: cache (ContextRepo) → GitLab source+diff enrichment → prompt →
LlmClient → cache write. Used by the pipeline (escalating prod alerts) and by
the /ai command (analyze_ref, on demand). /ai runs agentically: the model gets
read-only GitLab tools over the mapped service repos (crashing service first)
plus a Sentry related_errors(trace_id) tool to chase cross-service causes;
the pipeline keeps the cheaper one-shot prompt.
"""
from app.config import ENABLE_LLM_TOOLS, GITLAB_REF, log
from app.services.gitlab_tools import build_gitlab_server
from app.services.sentry_tools import build_sentry_server
from app.services.knowledge_tools import build_knowledge_server, NOTES_PROMPT
from app.services.llm import money
from app.utils import esc


class AnalysisService:
    def __init__(self, issues, context, gitlab, llm, sentry, audit=None,
                 knowledge=None):
        self._issues = issues      # IssuesRepo (resolve_ref)
        self._context = context    # ContextRepo (ctx + analysis cache)
        self._gitlab = gitlab      # GitLabClient
        self._llm = llm            # LlmClient
        self._sentry = sentry      # SentryApiClient (related_errors tool)
        self._audit = audit        # LlmAuditRepo (per-call audit trail)
        self._knowledge = knowledge  # KnowledgeService (notes memory tools)

    async def analyze(self, p: dict, use_tools: bool = False, chat_id=None):
        """Cause + fix for a parsed event. Returns text, or None if unavailable.
        Cached per issue+commit so repeats reuse the answer until the code changes.
        use_tools=True attaches the GitLab tool set (agentic loop, /ai path)."""
        issue_id = p.get("issue_id")
        blame_sha = (p.get("blame") or {}).get("sha_full") or "-"
        if self._llm.enabled and issue_id:
            row = self._context.get_analysis(issue_id, blame_sha)
            if row:
                log.info("llm cache hit issue=%s commit=%s", issue_id, blame_sha[:8])
                p["llm_meta"] = {"cached": True, "cost": row[1], "llm_id": row[2]}
                return row[0]

        # source window + the diff of the commit that last touched the crash line,
        # reusing the file located in the pipeline
        loc = p.get("_loc")
        blame = p.get("blame") or {}
        source = await self._gitlab.fetch_source(loc) if loc else None
        change = await self._gitlab.fetch_change(loc, blame.get("sha_full")) \
            if (loc and blame.get("sha_full")) else None

        chain = "\n".join(f"{t}: {v}" for t, v in (p.get("exc_chain") or [])) \
            or f"{p.get('type')}: {p.get('value')}"
        stack = "\n".join(p.get("frames_full") or p.get("frames") or [])
        prompt = (
            "You are a senior backend engineer triaging a Sentry error. Use the recent "
            "change diff to judge whether it introduced the bug. "
            "Reply in at most 4 short lines, plain text:\n"
            "Likely cause: <one sentence>\n"
            "Suggested fix: <one or two sentences>\n\n"
            f"Culprit: {p.get('culprit')}\n"
            f"Environment: {p.get('environment')}\n"
            + (f"Trace id: {p['trace_id']}\n" if p.get("trace_id") else "")
            + f"Exception chain (most recent last):\n{chain}\n\n"
            f"Stack (crash site first):\n{stack}"
        )
        if source:
            prompt += f"\n\nCurrent source around the crash site (from GitLab):\n{source}"
        if change:
            prompt += (
                f"\n\nMost recent change to this file "
                f"(commit {blame.get('sha')} by {blame.get('author')} on {blame.get('date')} — "
                f"\"{blame.get('subject')}\") — previous vs current (diff):\n{change}"
            )

        # agentic path (/ai): read-only GitLab tools over the mapped repos
        # (crashing service is the default), plus cross-service error lookup
        # by trace id, so the model can chase a cause into an upstream service
        servers = allowed = None
        if use_tools and ENABLE_LLM_TOOLS:
            repo = self._gitlab.repo_for(p)
            if repo:
                servers, allowed = build_gitlab_server(self._gitlab, repo)
                prompt += (
                    f"\n\nYou have read-only GitLab tools (read_file, find_file, "
                    f"search_code, blame, commit_diff, recent_commits) over the "
                    f"mapped service repos, ref {GITLAB_REF}; each takes a 'repo' "
                    f"argument defaulting to the crashing service's repo ({repo})."
                )
                s2, a2 = build_sentry_server(self._sentry)
                if s2:
                    servers.update(s2)
                    allowed = allowed + a2
                    prompt += (
                        " There is also related_errors(trace_id): error events "
                        "across ALL services on one trace — if the failure looks "
                        "caused by an upstream service (HTTP 5xx, timeout from a "
                        "downstream call), use it with the trace id above, then "
                        "read that service's repo."
                    )
                prompt += (
                    " Use the tools if the context above is not enough to be "
                    "confident. Keep the final answer in the format above, and "
                    "if the cause is in another service, name that service."
                )
            if servers and self._knowledge is not None:
                s3, a3 = build_knowledge_server(self._knowledge, source="ai")
                servers.update(s3)
                allowed = list(allowed) + a3
                prompt += NOTES_PROMPT

        # dump the prompt so you can inspect it (even while ENABLE_LLM is off)
        log.info("LLM prompt preview:\n%s", prompt)

        if not self._llm.enabled:
            return None

        rec = await self._llm.complete(prompt, mcp_servers=servers, allowed_tools=allowed)
        if rec is None:
            return None
        result = ("🤖 " + esc(rec.text)) if rec.text else None
        llm_id = self._audit.put("ai" if use_tools else "alert", rec,
                                 issue_id=issue_id, chat_id=chat_id) if self._audit else None
        p["llm_meta"] = {"cached": False, "cost": rec.cost, "in": rec.in_tokens,
                         "out": rec.out_tokens, "llm_id": llm_id}
        log.info("llm analysis issue=%s id=%s", issue_id, llm_id)
        if result and issue_id:
            self._context.put_analysis(issue_id, blame_sha, result, rec.cost, llm_id)
        return result

    async def analyze_ref(self, ref, chat_id=None):
        """Run (or reuse cached) analysis for an issue by id, on demand (/ai).
        Returns the analysis text, a status string, or None if the id is unknown."""
        info = self._issues.resolve_ref(ref)
        if not info:
            return None
        p = self._context.get_ctx(info["issue_id"])
        if p is None:
            return "Нет контекста для анализа — ошибка не приходила после запуска бота."
        if not self._llm.enabled:
            return "LLM выключен (ENABLE_LLM=false)."
        p["_loc"] = await self._gitlab.locate_source(p)
        if p.get("_loc"):
            p["blame"] = await self._gitlab.fetch_blame(p["_loc"])
        analysis = await self.analyze(p, use_tools=True, chat_id=chat_id)
        if not analysis:
            return "Пустой ответ от LLM."
        m = p.get("llm_meta") or {}
        if m.get("cached"):
            cost = f"💰 cached (~{money(m['cost'])})" if m.get("cost") else "💰 cached"
        else:
            toks = f"{m.get('in', 0)} in / {m.get('out', 0)} out"
            cost = f"💰 {money(m['cost'])} · {toks}" if m.get("cost") else f"💰 {toks}"
        if m.get("llm_id"):
            cost += f" · 🔍 <code>/llm {m['llm_id']}</code>"
        return f"{analysis}\n<i>{cost}</i>"
