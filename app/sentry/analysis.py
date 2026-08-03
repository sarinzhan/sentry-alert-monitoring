"""AnalysisService — the LLM cause/fix use-case.

Orchestrates: cache (ContextRepo) → GitLab source+diff enrichment → prompt →
LlmClient → cache write. Used by the pipeline (escalating prod alerts) and by
the /ai command (analyze_ref, on demand).
"""
from app.config import log
from app.services.llm import money
from app.utils import esc


class AnalysisService:
    def __init__(self, issues, context, gitlab, llm):
        self._issues = issues      # IssuesRepo (resolve_ref)
        self._context = context    # ContextRepo (ctx + analysis cache)
        self._gitlab = gitlab      # GitLabClient
        self._llm = llm            # LlmClient

    async def analyze(self, p: dict):
        """Cause + fix for a parsed event. Returns text, or None if unavailable.
        Cached per issue+commit so repeats reuse the answer until the code changes."""
        issue_id = p.get("issue_id")
        blame_sha = (p.get("blame") or {}).get("sha_full") or "-"
        if self._llm.enabled and issue_id:
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
        prompt = (
            "You are a senior backend engineer triaging a Sentry error. Use the recent "
            "change diff to judge whether it introduced the bug. "
            "Reply in at most 4 short lines, plain text:\n"
            "Likely cause: <one sentence>\n"
            "Suggested fix: <one or two sentences>\n\n"
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

        if not self._llm.enabled:
            return None

        res = await self._llm.complete(prompt)
        if res is None:
            return None
        text, in_tok, out_tok, cost = res
        result = ("🤖 " + esc(text)) if text else None
        p["llm_meta"] = {"cached": False, "cost": cost, "in": in_tok, "out": out_tok}
        log.info("llm analysis issue=%s", issue_id)   # auth/tokens logged by LlmClient
        if result and issue_id:
            self._context.put_analysis(issue_id, blame_sha, result, cost)
        return result

    async def analyze_ref(self, ref):
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
        analysis = await self.analyze(p)
        if not analysis:
            return "Пустой ответ от LLM."
        m = p.get("llm_meta") or {}
        if m.get("cached"):
            cost = f"💰 cached (~{money(m['cost'])})" if m.get("cost") else "💰 cached"
        else:
            toks = f"{m.get('in', 0)} in / {m.get('out', 0)} out"
            cost = f"💰 {money(m['cost'])} · {toks}" if m.get("cost") else f"💰 {toks}"
        return f"{analysis}\n<i>{cost}</i>"
