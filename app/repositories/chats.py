"""ChatsRepo — web-chat conversations («Вопрос LLM» tab).

A conversation binds a user to an SDK session id: every follow-up message is
sent with resume=session_id, so the model keeps the whole exchange in context.
chat_message rows exist only to render the chat in the UI (and keep the llm_id
audit link per answer) — the model's own memory is the session transcript.
All reads are scoped by username: users see only their own conversations.
"""
import time

_CONV_KEYS = ("id", "username", "session_id", "title", "created", "updated")
_MSG_KEYS = ("id", "role", "text", "llm_id", "at")


class ChatsRepo:
    def __init__(self, conn):
        self.db = conn

    def create(self, username, title):
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO chat_conversation(username, title, created, updated) "
            "VALUES (?, ?, ?, ?)", (username, (title or "").strip()[:80], now, now))
        self.db.commit()
        return cur.lastrowid

    def get(self, conv_id, username):
        """The conversation dict — only if it belongs to username."""
        row = self.db.execute(
            f"SELECT {', '.join(_CONV_KEYS)} FROM chat_conversation "
            "WHERE id=? AND username=?", (conv_id, username)).fetchone()
        return dict(zip(_CONV_KEYS, row)) if row else None

    def list_for(self, username, limit=50):
        rows = self.db.execute(
            "SELECT c.id, c.title, c.updated, COUNT(m.id) FROM chat_conversation c "
            "LEFT JOIN chat_message m ON m.conv_id = c.id "
            "WHERE c.username=? GROUP BY c.id ORDER BY c.updated DESC LIMIT ?",
            (username, limit)).fetchall()
        return [{"id": r[0], "title": r[1], "updated": r[2], "messages": r[3]}
                for r in rows]

    def set_session(self, conv_id, session_id):
        self.db.execute(
            "UPDATE chat_conversation SET session_id=?, updated=? WHERE id=?",
            (session_id, time.time(), conv_id))
        self.db.commit()

    def delete(self, conv_id, username):
        """Delete a conversation (and its messages) if it belongs to username."""
        if not self.get(conv_id, username):
            return 0
        self.db.execute("DELETE FROM chat_message WHERE conv_id=?", (conv_id,))
        cur = self.db.execute("DELETE FROM chat_conversation WHERE id=?", (conv_id,))
        self.db.commit()
        return cur.rowcount

    # ------------------------------------------------------------ messages
    def add_message(self, conv_id, role, text, llm_id=None):
        now = time.time()
        self.db.execute(
            "INSERT INTO chat_message(conv_id, role, text, llm_id, at) "
            "VALUES (?, ?, ?, ?, ?)", (conv_id, role, text, llm_id, now))
        self.db.execute("UPDATE chat_conversation SET updated=? WHERE id=?",
                        (now, conv_id))
        self.db.commit()

    def messages(self, conv_id):
        rows = self.db.execute(
            f"SELECT {', '.join(_MSG_KEYS)} FROM chat_message "
            "WHERE conv_id=? ORDER BY at, id", (conv_id,)).fetchall()
        return [dict(zip(_MSG_KEYS, r)) for r in rows]
