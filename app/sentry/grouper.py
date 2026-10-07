"""Grouper — decides which incident a freshly-ingested issue belongs to.

Org-scoped and cheap by construction:
  1. already grouped (repeat delivery / repeat event)  -> that incident
  2. exact signature match in an open incident          -> merge, no LLM
  3. no open incidents                                  -> new incident, no LLM
  4. otherwise ask the LLM whether it shares a root cause with an open one
LLM disabled or failing -> start a new incident (never wrongly merge).

Its own asyncio.Lock serializes the read-modify-write so two issues arriving at
once can't open duplicate incidents; the lock is separate from the pipeline's so
grouping never blocks the classic per-issue error path.
"""
import re
import json
import asyncio

from app.config import INCIDENT_GROUPING_MODEL, log
from app.sentry.grouping import signature


def _parse_target(text: str):
    """Pull {"incident_id": <int|null>} out of the model's reply. None = new."""
    m = re.search(r"\{[^{}]*\}", text or "")
    if not m:
        return None
    try:
        val = json.loads(m.group(0)).get("incident_id")
    except Exception:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.strip().isdigit():
        return int(val)
    return None


class Grouper:
    def __init__(self, incidents, llm, audit=None):
        self._inc = incidents
        self._llm = llm
        self._audit = audit
        self._lock = asyncio.Lock()

    async def assign(self, p, now, window_sec, max_members):
        """Returns {incident_id, is_lead, new_project}."""
        issue_id = p.get("issue_id")
        sig = signature(p)
        project = p.get("project")
        pid = p.get("project_id")
        project_id = str(pid) if pid is not None else None
        url = p.get("url")
        title = p.get("title") or p.get("type") or "?"
        short = p.get("short")

        async with self._lock:
            existing = self._inc.issue_incident(issue_id)
            if existing is not None:                       # idempotent
                self._inc.touch(existing, now)
                return {"incident_id": existing, "is_lead": False, "new_project": False}

            match = self._inc.signature_match(sig, now, window_sec)   # fast-path
            if match is not None:
                new_project = self._inc.add_member(match, issue_id, sig, project, title,
                                                   short, now, project_id=project_id, url=url)
                return {"incident_id": match, "is_lead": False, "new_project": new_project}

            openinc = self._inc.open_incidents(now, window_sec)
            if not openinc:                                # nothing to merge into
                iid = self._inc.create(issue_id, sig, project, title, short, now,
                                       project_id=project_id, url=url)
                return {"incident_id": iid, "is_lead": True, "new_project": True}

            target = await self._classify(p, openinc)      # LLM tie-breaker
            cur = next((o for o in openinc if o["incident_id"] == target), None)
            if cur is not None and cur["member_count"] < max_members:
                new_project = self._inc.add_member(target, issue_id, sig, project, title,
                                                   short, now, project_id=project_id, url=url)
                return {"incident_id": target, "is_lead": False, "new_project": new_project}

            iid = self._inc.create(issue_id, sig, project, title, short, now,
                                   project_id=project_id, url=url)
            return {"incident_id": iid, "is_lead": True, "new_project": True}

    async def _classify(self, p, openinc):
        """incident_id to merge into, or None for a new incident."""
        if not self._llm or not getattr(self._llm, "enabled", False):
            return None
        lines = []
        for o in openinc:
            desc = (o.get("description") or o.get("title") or "").replace("\n", " ")[:300]
            lines.append(f'- id={o["incident_id"]}: {desc} [projects: {o.get("projects") or "?"}]')
        prompt = (
            "You de-duplicate production errors into incidents. An incident groups "
            "errors that share ONE root cause (e.g. a downstream service is down), "
            "even when their exception type, message or location differ.\n\n"
            "Open incidents:\n" + "\n".join(lines) + "\n\n"
            "New error:\n"
            f"- type: {p.get('type') or p.get('title') or '?'}\n"
            f"- message: {(p.get('value') or '')[:300]}\n"
            f"- culprit: {p.get('culprit') or '?'}\n"
            f"- project: {p.get('project') or '?'}\n"
            + (f"- trace_id: {p['trace_id']}\n" if p.get('trace_id') else "")
            + "\nDoes this new error share a root cause with one of the open "
            'incidents? Reply with ONLY compact JSON: {"incident_id": <id>} to merge, '
            'or {"incident_id": null} to start a new incident. Prefer null when unsure.'
        )
        try:
            rec = await self._llm.complete(prompt, model=INCIDENT_GROUPING_MODEL)
        except Exception as e:
            log.warning("grouper classify failed: %s", e)
            return None
        if rec is None or not rec.text:
            return None
        if self._audit is not None:
            try:
                self._audit.put("group", rec, issue_id=p.get("issue_id"))
            except Exception:
                pass
        return _parse_target(rec.text)
