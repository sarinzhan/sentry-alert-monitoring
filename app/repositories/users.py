"""UsersRepo — web UI accounts: username/password, role, last activity.
Roles: admin — everything (users, projects, settings, prompts);
manager — investigation + history + editing the prompt presets
(role presets and problem templates); user — investigation + history only.
Passwords are stored as-is BY DESIGN: the admin screen shows and edits them
(internal tool behind the corporate network). Also owns the persisted
session-signing secret (meta table).
"""
import hmac
import secrets
import time

ROLES = ("admin", "manager", "user")

COLS = ("id", "username", "password", "role", "created", "last_activity",
        "api_token", "use_own_token")


class UsersRepo:
    def __init__(self, conn):
        self.db = conn
        self._secret = None
        # seed the first account so there's a way in on a fresh DB
        if not self.db.execute("SELECT 1 FROM auth_user LIMIT 1").fetchone():
            self.create("admin", "admin", "admin")
        # one-time migration: a draft schema briefly renamed full-rights
        # 'admin' to 'superadmin' (meta key roles_v2) and reused 'admin' for
        # the prompt-editor role, now called 'manager' — collapse both back
        if not self.db.execute(
                "SELECT 1 FROM meta WHERE key='roles_v3'").fetchone():
            if self.db.execute(
                    "SELECT 1 FROM meta WHERE key='roles_v2'").fetchone():
                self.db.execute(
                    "UPDATE auth_user SET role='manager' WHERE role='admin'")
            self.db.execute(
                "UPDATE auth_user SET role='admin' WHERE role='superadmin'")
            self.db.execute(
                "INSERT INTO meta(key, value) VALUES('roles_v3', '1')")
            self.db.commit()

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
        """Save (or clear, with None) the user's personal Claude token.
        Saving switches analyses onto it right away (that's what the user
        expects after pasting a token); clearing falls back to the shared one."""
        self.db.execute(
            "UPDATE auth_user SET api_token=?, use_own_token=? WHERE id=?",
            (token, 1 if token else 0, uid))
        self.db.commit()

    def set_use_own(self, uid, use_own):
        """Which token the user's analyses run on: 1 — personal, 0 — shared."""
        self.db.execute("UPDATE auth_user SET use_own_token=? WHERE id=?",
                        (1 if use_own else 0, uid))
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
