"""ChatMetaRepo — human-readable identity of each Telegram chat.

The bot upserts a row on every update it sees (title / @username / type), so the
web admin panel can list chats by name rather than a bare numeric id. Nothing in
the alerting path depends on this — it is display metadata only.
"""
import time


class ChatMetaRepo:
    def __init__(self, conn):
        self.db = conn

    def upsert(self, chat_id, title=None, username=None, type=None):
        """Record/refresh a chat's identity. Called on every incoming update."""
        self.db.execute(
            "INSERT OR REPLACE INTO chat_meta(chat_id, title, username, type, updated) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(chat_id), title, username, type, time.time()))
        self.db.commit()

    def all(self):
        """{chat_id: {title, username, type, updated}} for every known chat."""
        rows = self.db.execute(
            "SELECT chat_id, title, username, type, updated FROM chat_meta").fetchall()
        return {r[0]: {"title": r[1], "username": r[2], "type": r[3], "updated": r[4]}
                for r in rows}

    def get(self, chat_id):
        row = self.db.execute(
            "SELECT title, username, type, updated FROM chat_meta WHERE chat_id=?",
            (str(chat_id),)).fetchone()
        if not row:
            return None
        return {"title": row[0], "username": row[1], "type": row[2], "updated": row[3]}
