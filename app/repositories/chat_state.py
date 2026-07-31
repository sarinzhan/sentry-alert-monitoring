"""ChatStateRepo — per-chat send state: per (chat, issue) and per (chat, project).

Drives the per-chat min gap, critical rate limit, and the per-project window.
Read/written by the decision (app.sentry.decision) under the pipeline lock.
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

    def project_last_sent(self, chat_id, project):
        """When this chat last got any alert for this project (0.0 if never)."""
        row = self.db.execute(
            "SELECT last_sent FROM chat_project_state WHERE chat_id=? AND project=?",
            (str(chat_id), str(project))).fetchone()
        return row[0] if row else 0.0

    def remember_project(self, chat_id, project, last_sent):
        self.db.execute(
            "INSERT OR REPLACE INTO chat_project_state(chat_id, project, last_sent) "
            "VALUES (?, ?, ?)", (str(chat_id), str(project), last_sent))
        self.db.commit()
