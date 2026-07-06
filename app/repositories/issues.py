"""IssuesRepo — event log, occurrence counts, and per-issue metadata/state.

`event_log` holds one row per received event (global, shared by all chats);
`issue_state` holds the stable short id, title, url, project and a global
last_sent used by /status.
"""
import time

from app.config import STAT_WINDOWS, CRITICAL_WINDOW_SEC
from app.utils import short_id

# prune events older than the widest window we ever query
PRUNE_HORIZON = max(max(STAT_WINDOWS), CRITICAL_WINDOW_SEC)


class IssuesRepo:
    def __init__(self, conn):
        self.db = conn

    # ---------------------------------------------------------- event log
    def record_event(self, issue_id, usr=None, now=None):
        """Record one occurrence globally (issue-level) and prune old rows."""
        now = now or time.time()
        self.db.execute("INSERT INTO event_log (issue_id, ts, usr) VALUES (?, ?, ?)",
                        (issue_id, now, usr))
        self.db.execute("DELETE FROM event_log WHERE ts < ?", (now - PRUNE_HORIZON,))
        self.db.commit()

    def counts_for(self, issue_id, windows, now=None):
        """Occurrence counts for an issue over the given windows (seconds)."""
        now = now or time.time()
        return [self.db.execute(
            "SELECT COUNT(*) FROM event_log WHERE issue_id=? AND ts>=?", (issue_id, now - w)
        ).fetchone()[0] for w in windows]

    def crit_stats(self, issue_id, window_sec, now):
        """(occurrences, distinct affected users) for an issue over the critical window."""
        since = now - window_sec
        cnt = self.db.execute(
            "SELECT COUNT(*) FROM event_log WHERE issue_id=? AND ts>=?", (issue_id, since)
        ).fetchone()[0]
        usrs = self.db.execute(
            "SELECT COUNT(DISTINCT usr) FROM event_log WHERE issue_id=? AND ts>=? AND usr IS NOT NULL",
            (issue_id, since),
        ).fetchone()[0]
        return cnt, usrs

    # ---------------------------------------------------------- issue metadata
    def ensure_issue(self, issue_id, title):
        """Upsert issue metadata (stable short id + title) used by /status, /ai.
        Returns the short id. Does not touch send-state (that is per chat)."""
        short = short_id(issue_id)
        row = self.db.execute("SELECT 1 FROM issue_state WHERE issue_id=?", (issue_id,)).fetchone()
        now = time.time()
        if row:
            self.db.execute("UPDATE issue_state SET title=?, updated=? WHERE issue_id=?",
                            (title, now, issue_id))
        else:
            self.db.execute(
                "INSERT INTO issue_state(issue_id, last_sent, step, last_critical, short, title, updated) "
                "VALUES (?, 0, 0, 0, ?, ?, ?)", (issue_id, short, title, now))
        self.db.commit()
        return short

    def mark_sent(self, issue_id, now, critical=False):
        """Bump the issue's global last_sent (for /status), regardless of which chat sent."""
        self.db.execute(
            "UPDATE issue_state SET last_sent=?, step=step+1, updated=?"
            + (", last_critical=?" if critical else "") + " WHERE issue_id=?",
            ((now, now, now, issue_id) if critical else (now, now, issue_id)))
        self.db.commit()

    def cache_url_project(self, issue_id, url, project):
        """Cache the Sentry url + resolved project name for /status and /ai."""
        self.db.execute("UPDATE issue_state SET url=?, project=? WHERE issue_id=?",
                        (url, project, issue_id))
        self.db.commit()

    def resolve_ref(self, ref):
        """Look up an issue by its short id (#abc123) or raw issue id."""
        ref = (ref or "").strip().lstrip("#")
        if not ref:
            return None
        row = self.db.execute(
            "SELECT issue_id, short, title, project, url FROM issue_state "
            "WHERE short=? OR issue_id=? LIMIT 1", (ref.lower(), ref)).fetchone()
        if not row:
            return None
        return {"issue_id": row[0], "short": row[1], "title": row[2],
                "project": row[3], "url": row[4]}

    def issue_status(self, ref, windows=None):
        info = self.resolve_ref(ref)
        if not info:
            return None
        iid, now = info["issue_id"], time.time()
        info["counts"] = self.counts_for(iid, windows or STAT_WINDOWS, now)
        st = self.db.execute(
            "SELECT last_sent, last_critical FROM issue_state WHERE issue_id=?", (iid,)
        ).fetchone() or (0, 0)
        info["last_sent"], info["last_critical"] = st
        return info
