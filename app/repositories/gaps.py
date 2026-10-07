"""GapsRepo — LLM-reported observability gaps («Что нужно LLM»).

The model calls report_gap during an investigation when the data it needs isn't
there: no logs in Sentry, too few logs to reason, no source access, missing
context. One row per (project, kind) — re-reporting the same gap bumps its count
and recency rather than piling up duplicates. Read-only in the web panel; the
admin dismisses a row once addressed.
"""
import time

# recognized kinds -> human label (free-form kinds still accepted/shown as-is)
KINDS = {
    "no_logs_in_sentry": "Логов нет в Sentry",
    "insufficient_logs": "Мало логов для анализа",
    "no_source_access":  "Нет доступа к исходникам",
    "missing_context":   "Не хватает контекста",
    "other":             "Другое",
}


class GapsRepo:
    def __init__(self, conn):
        self.db = conn

    def report(self, project, kind, detail=None, issue_id=None, source=None, now=None):
        """Upsert one gap (project, kind). Returns the row id."""
        now = now or time.time()
        kind = (kind or "other").strip()[:40]
        project = (project or "").strip()[:120] or None
        cur = self.db.execute(
            "INSERT INTO llm_gap(project, kind, detail, issue_id, source, count,"
            " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, 1, ?, ?)"
            " ON CONFLICT(project, kind) DO UPDATE SET"
            " count=count+1, detail=excluded.detail, issue_id=excluded.issue_id,"
            " source=COALESCE(excluded.source, llm_gap.source), last_seen=excluded.last_seen",
            (project, kind, (detail or "").strip()[:1000] or None, issue_id, source, now, now))
        self.db.commit()
        return cur.lastrowid

    def list(self):
        """All gaps, most-recent first."""
        rows = self.db.execute(
            "SELECT id, project, kind, detail, issue_id, source, count, first_seen,"
            " last_seen FROM llm_gap ORDER BY last_seen DESC").fetchall()
        keys = ("id", "project", "kind", "detail", "issue_id", "source", "count",
                "first_seen", "last_seen")
        return [dict(zip(keys, r)) for r in rows]

    def delete(self, gap_id):
        cur = self.db.execute("DELETE FROM llm_gap WHERE id=?", (gap_id,))
        self.db.commit()
        return cur.rowcount
