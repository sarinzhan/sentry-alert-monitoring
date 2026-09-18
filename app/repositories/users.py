"""UsersRepo — web UI accounts: username/password, role (admin|user),
last activity. Passwords are stored as-is BY DESIGN: the admin screen shows
and edits them (internal tool behind the corporate network). Also owns the
persisted session-signing secret (meta table).
"""
import hmac
import secrets
import time

ROLES = ("admin", "user")

COLS = ("id", "username", "password", "role", "created", "last_activity",
        "api_token")


class UsersRepo:
    def __init__(self, conn):
        self.db = conn
        self._secret = None
        # seed the first account so there's a way in on a fresh DB
        if not self.db.execute("SELECT 1 FROM auth_user LIMIT 1").fetchone():
            self.create("admin", "admin", "admin")

    def secret(self):
        """Session-signing secret: generated once, persisted in meta."""
        if self._secret:
            return self._secret
        row = self.db.execute(
            "SELECT value FROM meta WHERE key='auth_secret'").fetchone()
        if row:
            self._secret = row[0]
        else:
            self._secret = secrets.token_hex(32)
            self.db.execute("INSERT INTO meta(key, value) VALUES('auth_secret', ?)",
                            (self._secret,))
            self.db.commit()
        return self._secret

    def _row(self, r):
        return dict(zip(COLS, r)) if r else None

    def get(self, username):
        return self._row(self.db.execute(
            f"SELECT {', '.join(COLS)} FROM auth_user WHERE username=?",
            (username,)).fetchone())

    def get_by_id(self, uid):
        return self._row(self.db.execute(
            f"SELECT {', '.join(COLS)} FROM auth_user WHERE id=?",
            (uid,)).fetchone())

    def verify(self, username, password):
        """Constant-time password check; returns the user row or None."""
        user = self.get((username or "").strip())
        if user and hmac.compare_digest(user["password"], password or ""):
            return user
        return None

    def list(self):
        rows = self.db.execute(
            f"SELECT {', '.join(COLS)} FROM auth_user ORDER BY username").fetchall()
        return [self._row(r) for r in rows]

    def create(self, username, password, role="user"):
        cur = self.db.execute(
            "INSERT INTO auth_user(username, password, role, created) "
            "VALUES (?, ?, ?, ?)", (username, password, role, time.time()))
        self.db.commit()
        return self.get_by_id(cur.lastrowid)

    def update(self, uid, username=None, password=None, role=None):
        """Change any of username/password/role; None leaves the field as is."""
        user = self.get_by_id(uid)
        if user is None:
            return None
        self.db.execute(
            "UPDATE auth_user SET username=?, password=?, role=? WHERE id=?",
            (username if username is not None else user["username"],
             password if password is not None else user["password"],
             role if role is not None else user["role"],
             uid))
        self.db.commit()
        return self.get_by_id(uid)

    def set_token(self, uid, token):
        """Save (or clear, with None) the user's personal Claude token."""
        self.db.execute("UPDATE auth_user SET api_token=? WHERE id=?",
                        (token, uid))
        self.db.commit()

    def delete(self, uid):
        self.db.execute("DELETE FROM auth_user WHERE id=?", (uid,))
        self.db.commit()

    def touch(self, username):
        """Record activity — shown as «последняя активность» in the admin UI."""
        self.db.execute("UPDATE auth_user SET last_activity=? WHERE username=?",
                        (time.time(), username))
        self.db.commit()

    def admin_count(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM auth_user WHERE role='admin'").fetchone()[0]
