"""AnalysisService — the LLM cause/fix use-case.

Orchestrates: cache (ContextRepo) → GitLab source+diff enrichment → prompt →
LlmClient → cache write. Used by the pipeline (every alert) and by the /ai
command (analyze_ref, on demand). /ai runs agentically: the model gets
read-only GitLab tools over the mapped service repos (crashing service first)
plus a Sentry related_errors(trace_id) tool to chase cross-service causes;
the pipeline keeps the cheaper one-shot prompt.
"""
from app.config import ENABLE_LLM_TOOLS, GITLAB_REF, log
from app.services.gitlab_tools import build_gitlab_server
from app.services.sentry_tools import build_sentry_server, fmt_event_details
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

    async def analyze(self, p: dict, use_tools: bool = False, chat_id=None,
                      kind: str = "alert"):
        """Cause + fix for a parsed event. Returns text, or None if unavailable.
        Cached per issue+commit so repeats reuse the answer until the code changes.
        use_tools=True attaches the agentic tool set (GitLab + Sentry + notes).
        kind labels the audit row and the notes source ('alert' or 'ai')."""
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

        # the webhook payload is often thin (issue-type payloads carry no
        # stacktrace); pull the full latest event for tags, breadcrumbs (the
        # user's preceding actions), request and the real stack. Best-effort.
        event_detail = None
        if self._sentry is not None and issue_id:
            ev = await self._sentry.latest_event(issue_id)
            if ev:
                event_detail = fmt_event_details(ev)
                p.setdefault("trace_id",
                             ((ev.get("contexts") or {}).get("trace") or {}).get("trace_id"))

        chain = "\n".join(f"{t}: {v}" for t, v in (p.get("exc_chain") or [])) \
            or f"{p.get('type')}: {p.get('value')}"
        stack = "\n".join(p.get("frames_full") or p.get("frames") or [])
        prompt = (
            "You are a senior backend engineer triaging a Sentry error for a mobile "
            "operator. Use the recent change diff to judge whether it introduced the "
            "bug. Answer in RUSSIAN, plain text, exactly three short lines, no jargon "
            "a non-engineer wouldn't get:\n"
            "Что сломалось: <что именно не работает у пользователя — напр. не грузится "
            "приложение, нельзя авторизоваться, не подключается пакет>\n"
            "Причина: <почему — изменение кода (кто/какой коммит), некорректный "
            "запрос, проблема данных и т.п.; если не точно — самая вероятная версия>\n"
            "Исправление: <1–2 предложения, что сделать>\n\n"
            f"Service: {p.get('project') or '?'}\n"
            f"Title: {p.get('title') or '?'}\n"
            f"Culprit: {p.get('culprit')}\n"
            f"Environment: {p.get('environment') or '?'}\n"
            f"Severity: level {p.get('level') or '?'}, {p.get('count') or '?'} events, "
            f"{p.get('user_count') or '?'} affected users\n"
            + (f"Trace id: {p['trace_id']}\n" if p.get("trace_id") else "")
            + f"Exception chain (most recent last):\n{chain}\n\n"
            f"Stack (crash site first):\n{stack or '(empty — see the full event below)'}"
        )
        if event_detail:
            prompt += ("\n\nFull latest event from Sentry (tags, breadcrumbs = the "
                       f"user's preceding actions, request):\n{event_detail}")
        if source:
            prompt += f"\n\nCurrent source around the crash site (from GitLab):\n{source}"
        if change:
            prompt += (
                f"\n\nMost recent change to this file "
                f"(commit {blame.get('sha')} by {blame.get('author')} on {blame.get('date')} — "
                f"\"{blame.get('subject')}\") — previous vs current (diff):\n{change}"
            )

        # tool-enabled path: GitLab tools when the service repo is mapped, PLUS
        # Sentry tools regardless of mapping (cross-service error lookup by trace,
        # event lookups, and application-LOG search), PLUS the notes memory — so
        # the model can look things up when the embedded source+diff isn't enough.
        servers, allowed = {}, []
        if use_tools and ENABLE_LLM_TOOLS:
            repo = self._gitlab.repo_for(p)
            if repo:
                gs, ga = build_gitlab_server(self._gitlab, repo)
                servers.update(gs)
                allowed += ga
                prompt += (
                    f"\n\nYou have read-only GitLab tools (read_file, find_file, "
                    f"search_code, blame, commit_diff, recent_commits) over the "
                    f"mapped service repos, ref {GITLAB_REF}; each takes a 'repo' "
                    f"argument defaulting to the crashing service's repo ({repo})."
                )
            ss, sa = build_sentry_server(self._sentry)
            if ss:
                servers.update(ss)
                allowed += sa
                prompt += (
                    "\n\nYou have Sentry tools: find_events (by request_id / "
                    "device_id / msisdn), related_errors(trace_id) to chase a "
                    "failure upstream across services (use the trace id above when "
                    "it looks caused by a downstream HTTP 5xx / timeout), "
                    "user_events, event_details, and search_logs over application "
                    "LOG lines (INFO too, not just errors)."
                )
            if servers and self._knowledge is not None:
                ks, ka = build_knowledge_server(self._knowledge, source=kind)
                servers.update(ks)
                allowed += ka
                prompt += NOTES_PROMPT
            if servers:
                prompt += (
                    "\n\nUse the tools if the context above is not enough to be "
                    "confident. Keep the final answer in the exact format above, "
                    "and if the cause is in another service, name that service."
                )

        # dump the prompt so you can inspect it (even while ENABLE_LLM is off)
        log.info("LLM prompt preview:\n%s", prompt)

        if not self._llm.enabled:
            return None

        rec = await self._llm.complete(prompt, mcp_servers=servers or None,
                                       allowed_tools=allowed or None)
        if rec is None:
            return None
        result = ("🤖 " + esc(rec.text)) if rec.text else None
        llm_id = self._audit.put(kind, rec,
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
        analysis = await self.analyze(p, use_tools=True, chat_id=chat_id, kind="ai")
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
