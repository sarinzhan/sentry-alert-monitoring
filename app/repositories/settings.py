"""SettingsRepo — runtime settings the admin edits in the web UI, stored in
the meta key-value table: the general system prompt and the daily LLM limits
(the env values LLM_DAILY_LIMIT / LLM_DAILY_TOKENS are only the defaults)."""


class SettingsRepo:
    def __init__(self, conn):
        self.db = conn

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?",
                              (key,)).fetchone()
        return row[0] if row and row[0] is not None else default

    def get_int(self, key, default=0):
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return default

    def set(self, key, value):
        """Store a value; None removes the key (falls back to the default)."""
        if value is None:
            self.db.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            self.db.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
        self.db.commit()
