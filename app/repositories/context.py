"""ContextRepo — parsed-context snapshots, cached LLM analysis, and sent-message map.

- issue_ctx:      last parsed context per issue, so /ai can rebuild the prompt.
- analysis_cache: LLM answer per issue+commit, so repeats reuse it until code changes.
- sent_message:   sent alert -> issue map, for reply-based commands.
"""
import json
import time

_CTX_KEYS = ("issue_id", "project_id", "project", "title", "culprit", "environment",
             "type", "value", "exc_chain", "frames_full", "frames_struct", "url", "short",
             "event_id", "trace_id", "tags", "msisdn", "usr", "timestamp")


class ContextRepo:
    def __init__(self, conn):
        self.db = conn

    # ------------------------------------------------------------ issue_ctx
    def store_ctx(self, p):
        """Persist the parsed context so /ai can rebuild the prompt later."""
        keep = {k: p.get(k) for k in _CTX_KEYS}
        self.db.execute("INSERT OR REPLACE INTO issue_ctx(issue_id, ctx, at) VALUES (?, ?, ?)",
                        (p["issue_id"], json.dumps(keep, default=str), time.time()))
        self.db.commit()

    def get_ctx(self, issue_id):
        row = self.db.execute("SELECT ctx FROM issue_ctx WHERE issue_id=?", (issue_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # ------------------------------------------------------------ analysis_cache
    def get_analysis(self, issue_id, blame_sha):
        """(analysis, cost) for this issue+commit, or None."""
        return self.db.execute(
            "SELECT analysis, cost FROM analysis_cache WHERE issue_id=? AND blame_sha=?",
            (issue_id, blame_sha)).fetchone()

    def put_analysis(self, issue_id, blame_sha, analysis, cost):
        self.db.execute(
            "INSERT OR REPLACE INTO analysis_cache(issue_id, blame_sha, analysis, cost, updated) "
            "VALUES (?, ?, ?, ?, ?)", (issue_id, blame_sha, analysis, cost, time.time()))
        self.db.commit()

    # ------------------------------------------------------------ sent_message
    def store_sent(self, message_id, chat_id, issue_id, short):
        """Map a sent alert -> issue (for reply-based commands). Pruned after 30 days."""
        now = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO sent_message(message_id, chat_id, issue_id, short, at) "
            "VALUES (?, ?, ?, ?, ?)", (message_id, chat_id, issue_id, short, now))
        self.db.execute("DELETE FROM sent_message WHERE at < ?", (now - 30 * 86400,))
        self.db.commit()

    def resolve_reply(self, message_id):
        """Find the issue behind an alert message the user replied to."""
        row = self.db.execute(
            "SELECT s.issue_id, s.short, i.title, i.project, i.url FROM sent_message s "
            "LEFT JOIN issue_state i ON i.issue_id=s.issue_id WHERE s.message_id=?",
            (message_id,)).fetchone()
        if not row:
            return None
        return {"issue_id": row[0], "short": row[1], "title": row[2],
                "project": row[3], "url": row[4]}
