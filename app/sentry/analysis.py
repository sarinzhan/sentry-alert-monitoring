"""AnalysisService — the LLM cause/fix use-case.

Orchestrates: cache (ContextRepo) → GitLab source+diff enrichment → prompt →
bounded agent run (AgentService.analyze_run) → cache write. Used by the
pipeline (escalating prod alerts); /ai now runs the deeper AgentService.investigate.
"""
from app.config import log
from app.services.claude_agent import money
from app.utils import esc


class AnalysisService:
    def __init__(self, issues, context, gitlab, agent):
        self._issues = issues      # IssuesRepo (resolve_ref)
        self._context = context    # ContextRepo (ctx + analysis cache)
        self._gitlab = gitlab      # GitLabClient
        self._agent = agent        # AgentService (analyze_run)

    async def analyze(self, p: dict):
        """Cause + fix for a parsed event. Returns text, or None if unavailable.
        Cached per issue+commit so repeats reuse the answer until the code changes."""
        issue_id = p.get("issue_id")
        blame_sha = (p.get("blame") or {}).get("sha_full") or "-"
        if self._agent.enabled and issue_id:
            row = self._context.get_analysis(issue_id, blame_sha)
            if row:
                log.info("llm cache hit issue=%s commit=%s", issue_id, blame_sha[:8])
                p["llm_meta"] = {"cached": True, "cost": row[1]}
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
        # the triage instructions live in ANALYSIS_SYSTEM_PROMPT (agent.py);
        # this is just the evidence
        prompt = (
            f"Culprit: {p.get('culprit')}\n"
            f"Environment: {p.get('environment')}\n"
            f"Exception chain (most recent last):\n{chain}\n\n"
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

        # dump the prompt so you can inspect it (even while ENABLE_LLM is off)
        log.info("LLM prompt preview:\n%s", prompt)

        if not self._agent.enabled:
            return None

        ans = await self._agent.analyze_run(prompt)
        if ans is None:
            return None
        result = ("🤖 " + esc(ans.text)) if ans.text else None
        p["llm_meta"] = {"cached": False, "cost": ans.cost,
                         "in": ans.in_tokens, "out": ans.out_tokens}
        log.info("llm call issue=%s in=%d out=%d cost=$%.4f%s", issue_id,
                 ans.in_tokens, ans.out_tokens, ans.cost,
                 " (est)" if ans.cost_estimated else "")
        if result and issue_id:
            self._context.put_analysis(issue_id, blame_sha, result, ans.cost)
        return result

    async def analyze_ref(self, ref):
        """Run (or reuse cached) analysis for an issue by id, on demand.
        Kept for the cached quick-look path (cheaper than a full /ai investigation).
        Returns the analysis text, a status string, or None if the id is unknown."""
        info = self._issues.resolve_ref(ref)
        if not info:
            return None
        p = self._context.get_ctx(info["issue_id"])
        if p is None:
            return "Нет контекста для анализа — ошибка не приходила после запуска бота."
        if not self._agent.enabled:
            return "LLM выключен (ENABLE_LLM=false или нет ключа/токена)."
        p["_loc"] = await self._gitlab.locate_source(p)
        if p.get("_loc"):
            p["blame"] = await self._gitlab.fetch_blame(p["_loc"])
        analysis = await self.analyze(p)
        if not analysis:
            return "Пустой ответ от LLM."
        m = p.get("llm_meta") or {}
        if m.get("cached"):
            cost = f"💰 cached (~{money(m['cost'])})" if m.get("cost") else "💰 cached"
        else:
            cost = f"💰 {money(m.get('cost', 0))} · {m.get('in', 0)} in / {m.get('out', 0)} out"
        return f"{analysis}\n<i>{cost}</i>"
