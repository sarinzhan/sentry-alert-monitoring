"""UserMapRepo — map a VCS commit-author name to a Telegram handle (@mention)."""
import time


class UserMapRepo:
    def __init__(self, conn):
        self.db = conn

    def add(self, vcs, telegram, by):
        vcs = (vcs or "").strip().lower()
        telegram = (telegram or "").strip().lstrip("@")
        if not vcs or not telegram:
            return False
        self.db.execute("INSERT OR REPLACE INTO user_map(vcs, telegram, by, at) VALUES (?, ?, ?, ?)",
                        (vcs, telegram, by, time.time()))
        self.db.commit()
        return True

    def delete(self, vcs):
        cur = self.db.execute("DELETE FROM user_map WHERE vcs=?", ((vcs or "").strip().lower(),))
        self.db.commit()
        return cur.rowcount

    def list_all(self):
        return self.db.execute("SELECT vcs, telegram FROM user_map ORDER BY vcs").fetchall()

    def lookup(self, author):
        if not author:
            return None
        row = self.db.execute("SELECT telegram FROM user_map WHERE vcs=?",
                              (author.strip().lower(),)).fetchone()
        return row[0] if row else None
