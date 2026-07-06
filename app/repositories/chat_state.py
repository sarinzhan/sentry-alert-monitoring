"""ChatStateRepo — per (chat, issue) send state.

Drives the per-chat min gap and critical rate limit. Read/written by the
decision (app.sentry.decision) under the pipeline lock.
"""


class ChatStateRepo:
    def __init__(self, conn):
        self.db = conn

    def get(self, chat_id, issue_id):
        """(last_sent, last_critical, step) or None if this chat hasn't seen the issue."""
        return self.db.execute(
            "SELECT last_sent, last_critical, step FROM chat_issue_state "
            "WHERE chat_id=? AND issue_id=?", (str(chat_id), issue_id)).fetchone()

    def remember(self, chat_id, issue_id, last_sent, last_critical, step):
        self.db.execute(
            "INSERT OR REPLACE INTO chat_issue_state(chat_id, issue_id, last_sent, last_critical, step) "
            "VALUES (?, ?, ?, ?, ?)", (str(chat_id), issue_id, last_sent, last_critical, step))
        self.db.commit()
