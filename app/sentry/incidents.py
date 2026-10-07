"""IncidentService — the incident lifecycle.

group (via Grouper) -> threshold-gated notify per chat -> edit in place as it
grows (+ re-ping on escalation) -> resolve (Resolved button or idle sweeper),
which deletes the live message(s) and posts a short resolved summary.

Grouping is org-scoped; notification is per-chat (each chat's own incident_*
thresholds decide when an incident becomes significant enough to page it, scoped
to the chat's subscribed projects). The classic per-issue error path is untouched.
"""
import time
import asyncio

from app.config import INCIDENT_WINDOW_SEC, INCIDENT_MAX_MEMBERS, log
from app.sentry.message import (build_incident_message, build_incident_resolved,
                                 title_from_analysis)


def _as_chat_id(chat_id):
    """Telegram accepts int ids for groups/users and str for @channels."""
    s = str(chat_id)
    try:
        return int(s)
    except ValueError:
        return s


class IncidentService:
    def __init__(self, *, incidents, grouper, issues, rules, subscriptions,
                 analysis, context, bot):
        self._inc = incidents
        self._grouper = grouper
        self._issues = issues
        self._rules = rules
        self._subs = subscriptions
        self._analysis = analysis
        self._context = context
        self._bot = bot
        # serializes the notify/edit/resolve side so concurrent burst events for
        # one incident can't post the "first" message twice to the same chat
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ ingestion
    async def handle(self, p, targets, get_analysis, now=None):
        """Group this issue, then notify/edit incident-enabled chats among `targets`
        (the project's subscribers). `get_analysis` lazily yields the shared LLM
        analysis text, reused as the incident's root-cause description."""
        now = now or time.time()
        g = await self._grouper.assign(p, now, INCIDENT_WINDOW_SEC, INCIDENT_MAX_MEMBERS)
        incident_id = g["incident_id"]
        async with self._lock:
            # a repeat event of an already-resolved incident's issue maps back here
            # (idempotency) — never re-notify a closed incident
            cur = self._inc.get(incident_id)
            if not cur or cur["status"] != "open":
                return incident_id
            for chat_id, thread_id in targets:
                rules = self._rules.effective(chat_id)
                if not rules.get("incident_enabled"):
                    continue
                try:
                    await self._notify_chat(incident_id, chat_id, thread_id, rules, g, p,
                                            get_analysis, now)
                except Exception as e:
                    log.warning("incident notify failed inc=%s chat=%s: %s",
                                incident_id, chat_id, e)
        return incident_id

    def _scope_ids(self, chat_id, members):
        """Member issue_ids in THIS chat's subscribed projects — so significance
        reflects the chat's own blast radius, not the whole cross-service total.
        A '*' (all-projects) subscription matches every member."""
        subs = set(self._subs.list_for(chat_id))
        if "*" in subs:
            return [m["issue_id"] for m in members]
        return [m["issue_id"] for m in members
                if str(m.get("project_id") or "") in subs or (m.get("project") or "") in subs]

    async def _notify_chat(self, incident_id, chat_id, thread_id, rules, g, p, get_analysis, now):
        window = rules.get("incident_window_sec") or INCIDENT_WINDOW_SEC
        err_thr = rules.get("incident_error_threshold")
        usr_thr = rules.get("incident_user_threshold")
        scope_ids = self._scope_ids(chat_id, self._inc.members(incident_id))
        events, users = self._issues.agg_stats(scope_ids, window, now)
        significant = ((err_thr is not None and events > err_thr) or
                       (usr_thr is not None and users >= usr_thr))
        thread = int(thread_id) if thread_id else None
        im = self._inc.message_for(incident_id, chat_id)

        # first exposure of this chat to the incident — only once it's significant
        if im is None:
            if not significant:
                return
            analysis = await get_analysis()
            inc = self._inc.get(incident_id)
            if analysis and not inc.get("description"):
                # title from the LLM («Что сломалось» line); keep the raw title if absent
                llm_title = title_from_analysis(analysis) or None
                self._inc.set_description(incident_id, analysis, title=llm_title)
                inc = self._inc.get(incident_id)
            members = self._inc.members(incident_id)
            text = build_incident_message(inc, members, analysis, events, users, p.get("url"))
            msg = await self._bot.send(text, chat_id=_as_chat_id(chat_id),
                                       message_thread_id=thread,
                                       reply_markup=self._bot.resolve_markup(incident_id))
            if msg is not None:
                self._inc.record_message(incident_id, chat_id, msg.message_id, thread_id,
                                         inc["member_count"], now)
            return

        # already live for this chat — edit in place to the current state
        inc = self._inc.get(incident_id)
        members = self._inc.members(incident_id)
        analysis = inc.get("description")
        text = build_incident_message(inc, members, analysis, events, users, p.get("url"))
        if im.get("message_id"):
            try:
                await self._bot.edit(text, chat_id=_as_chat_id(chat_id),
                                     message_id=im["message_id"], message_thread_id=thread,
                                     reply_markup=self._bot.resolve_markup(incident_id))
            except Exception as e:
                log.debug("incident edit skipped inc=%s chat=%s: %s", incident_id, chat_id, e)

        # re-ping on escalation: a new project joined, or members at least doubled
        prev = im.get("member_count_at_send") or 1
        escalated = g.get("new_project") or (inc["member_count"] > prev and inc["member_count"] >= prev * 2)
        if escalated:
            note = build_incident_message(inc, members, analysis, events, users, p.get("url"),
                                          escalation=True)
            msg = await self._bot.send(note, chat_id=_as_chat_id(chat_id),
                                       message_thread_id=thread,
                                       reply_markup=self._bot.resolve_markup(incident_id))
            if msg is not None:                       # the fresh message becomes the live one
                self._inc.record_message(incident_id, chat_id, msg.message_id, thread_id,
                                         inc["member_count"], now)
                self._inc.mark_escalated(incident_id, now)
        else:
            self._inc.update_message_count(incident_id, chat_id, inc["member_count"])

    # ------------------------------------------------------------ resolution
    def get(self, incident_id):
        return self._inc.get(incident_id)

    async def request_resolve(self, incident_id, by):
        """Resolved button tapped: run verification, then either close immediately
        (code fix confirmed in main) or ask the resolver for the solution text.
        Returns {"status": "resolved"|"need_text"|"gone", "verdict": {...}}."""
        inc = self._inc.get(incident_id)
        if not inc or inc["status"] != "open":
            return {"status": "gone"}
        verdict = await self._verify(inc)
        if verdict.get("verified") and not verdict.get("needs_solution_text"):
            await self.resolve(incident_id, by=by, resolution=verdict.get("evidence"),
                               kind="manual", now=time.time())
            return {"status": "resolved", "verdict": verdict}
        return {"status": "need_text", "verdict": verdict}

    async def finalize_resolve(self, incident_id, by, text, verdict=None):
        """Close after the resolver supplied the solution text."""
        parts = [x for x in (text, (verdict or {}).get("evidence")) if x]
        resolution = "\n".join(parts) if parts else None
        kind = "manual" if (verdict and verdict.get("verified")) else "unverified"
        return await self.resolve(incident_id, by=by, resolution=resolution, kind=kind,
                                  now=time.time())

    async def resolve(self, incident_id, by=None, resolution=None, kind="manual", now=None):
        now = now or time.time()
        async with self._lock:
            inc = self._inc.get(incident_id)
            if not inc or inc["status"] != "open":
                return False
            members = self._inc.members(incident_id)
            msgs = self._inc.all_messages(incident_id)
            self._inc.resolve(incident_id, by, resolution, kind, now)
            inc = self._inc.get(incident_id)
        summary = build_incident_resolved(inc, members, now)
        for m in msgs:
            try:
                if m.get("message_id"):
                    await self._bot.delete(chat_id=_as_chat_id(m["chat_id"]),
                                           message_id=m["message_id"])
            except Exception as e:
                log.debug("incident delete skipped inc=%s chat=%s: %s",
                          incident_id, m["chat_id"], e)
            await self._bot.send(summary, chat_id=_as_chat_id(m["chat_id"]),
                                 message_thread_id=int(m["thread_id"]) if m.get("thread_id") else None)
        self._inc.delete_messages(incident_id)
        log.info("incident resolved inc=%s by=%s kind=%s", incident_id, by, kind)
        return True

    async def sweep(self, now=None):
        """Auto-resolve incidents idle for a full window (the TTL timeout)."""
        now = now or time.time()
        due = self._inc.sweep_candidates(now, INCIDENT_WINDOW_SEC)
        mins = max(1, int(INCIDENT_WINDOW_SEC // 60))
        for iid in due:
            await self.resolve(iid, by=None, resolution=f"нет новых ошибок {mins} мин",
                               kind="auto", now=now)
        return len(due)

    # ------------------------------------------------------------ verification
    async def _verify(self, inc):
        """Agentic check: is the fix in main (code) / are the logs clean (network)?"""
        fallback = {"class": "unknown", "verified": False, "evidence": None,
                    "needs_solution_text": True}
        if self._analysis is None:
            return fallback
        p = self._context.get_ctx(inc["lead_issue_id"]) if self._context else None
        try:
            verdict = await self._analysis.verify_resolution(p)
        except Exception as e:
            log.warning("incident verify failed inc=%s: %s", inc["incident_id"], e)
            return fallback
        return verdict or fallback
