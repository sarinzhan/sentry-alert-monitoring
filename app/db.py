"""Database — owns the single SQLite connection and the full schema.

All tables and additive migrations live here so the shape of the DB is described
in one place. Repositories (app.repositories.*) receive `Database.conn` and hold
only queries. Writes that must be atomic across reads are serialized by the
pipeline's asyncio.Lock, not here.
"""
import sqlite3

from app.config import DB_PATH
from app.utils import short_id


class Database:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._create_schema()

    def close(self):
        self.conn.close()

    def _create_schema(self):
        db = self.conn
        # per-issue send bookkeeping + metadata (short id, title, url, project)
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS issue_state (
                issue_id  TEXT PRIMARY KEY,
                last_sent REAL    NOT NULL,
                step      INTEGER NOT NULL,
                title     TEXT,
                updated   REAL    NOT NULL
            )
            """
        )
        # one row per received event -> count occurrences (and distinct users) per issue
        db.execute("CREATE TABLE IF NOT EXISTS event_log (issue_id TEXT NOT NULL, ts REAL NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS ix_event_log ON event_log (issue_id, ts)")
        # additive migrations (ignore 'duplicate column' on re-run)
        for stmt in (
            "ALTER TABLE issue_state ADD COLUMN last_critical REAL DEFAULT 0",
            "ALTER TABLE issue_state ADD COLUMN short TEXT",
            "ALTER TABLE issue_state ADD COLUMN url TEXT",
            "ALTER TABLE issue_state ADD COLUMN project TEXT",
            "ALTER TABLE event_log ADD COLUMN usr TEXT",
        ):
            try:
                db.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # backfill the stable short id for pre-existing issues
        for (iid,) in db.execute("SELECT issue_id FROM issue_state WHERE short IS NULL").fetchall():
            db.execute("UPDATE issue_state SET short=? WHERE issue_id=?", (short_id(iid), iid))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_issue_short ON issue_state(short)")

        # sent alert -> issue mapping (reply-based commands) + keyword force-send list
        db.execute(
            "CREATE TABLE IF NOT EXISTS sent_message ("
            " message_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,"
            " issue_id TEXT NOT NULL, short TEXT, at REAL NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS keyword ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,"
            " project TEXT, by TEXT, at REAL NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS ix_keyword_proj ON keyword(project)")
        # vcs author name -> telegram handle (to @mention the culprit in the alert)
        db.execute(
            "CREATE TABLE IF NOT EXISTS user_map ("
            " vcs TEXT PRIMARY KEY, telegram TEXT NOT NULL, by TEXT, at REAL NOT NULL)"
        )
        # last parsed context per issue, so /ai can re-run the LLM on demand
        db.execute(
            "CREATE TABLE IF NOT EXISTS issue_ctx ("
            " issue_id TEXT PRIMARY KEY, ctx TEXT NOT NULL, at REAL NOT NULL)"
        )
        # cached LLM analysis per issue, keyed also by the crash-line commit so it
        # invalidates when the code changes.
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_cache (
                issue_id  TEXT PRIMARY KEY,
                blame_sha TEXT,
                analysis  TEXT NOT NULL,
                cost      REAL,
                updated   REAL NOT NULL
            )
            """
        )
        for stmt in (
            "ALTER TABLE analysis_cache ADD COLUMN cost REAL",
            "ALTER TABLE analysis_cache ADD COLUMN llm_id TEXT",
        ):
            try:
                db.execute(stmt)
            except sqlite3.OperationalError:
                pass

        # full audit of every LLM call: what it got, what tools it used, what it
        # answered. Keyed by a short id shown in the reply (/llm <id>, GET /api/llm/{id}).
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_call (
                id          TEXT PRIMARY KEY,
                at          REAL NOT NULL,
                kind        TEXT NOT NULL,
                issue_id    TEXT,
                chat_id     TEXT,
                model       TEXT,
                auth        TEXT,
                agentic     INTEGER,
                prompt      TEXT,
                tools_offered TEXT,
                tool_calls  TEXT,
                turns       INTEGER,
                in_tokens   INTEGER,
                out_tokens  INTEGER,
                cost_usd    REAL,
                duration_ms INTEGER,
                response    TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_at ON llm_call(at)")

        # generated API documentation cache (/api command), keyed by the
        # normalized query the user typed.
        db.execute(
            "CREATE TABLE IF NOT EXISTS api_doc ("
            " query TEXT PRIMARY KEY, doc TEXT NOT NULL, llm_id TEXT, at REAL NOT NULL)"
        )

        # project catalog: sentry project id -> display name + gitlab repo.
        # Seeded from SENTRY_PROJECTS / GITLAB_PROJECTS env on startup, then the
        # DB is the source of truth (edited in the web UI); unknown projects
        # seen in webhooks/API get auto-registered with empty fields.
        db.execute(
            "CREATE TABLE IF NOT EXISTS project ("
            " id TEXT PRIMARY KEY, name TEXT, gitlab_repo TEXT,"
            " first_seen REAL NOT NULL, updated REAL NOT NULL)"
        )
        try:
            # the sentry-side slug, auto-filled from the API (read-only in the
            # web UI; `name` stays the user-editable display name)
            db.execute("ALTER TABLE project ADD COLUMN slug TEXT")
        except sqlite3.OperationalError:
            pass

        # --- multi-chat: subscriptions, per-chat rules, per (chat, issue) send state ---
        db.execute(
            "CREATE TABLE IF NOT EXISTS chat_subscription ("
            " chat_id TEXT NOT NULL, project TEXT NOT NULL, thread_id TEXT,"
            " by TEXT, at REAL, PRIMARY KEY (chat_id, project))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS chat_rules ("
            " chat_id TEXT PRIMARY KEY, statuses TEXT, ongoing_sec INTEGER,"
            " critical_window_sec INTEGER, critical_threshold INTEGER,"
            " affected_user_threshold INTEGER, critical_ratelimit_sec INTEGER,"
            " stat_windows TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS chat_issue_state ("
            " chat_id TEXT NOT NULL, issue_id TEXT NOT NULL,"
            " last_sent REAL NOT NULL DEFAULT 0, last_critical REAL NOT NULL DEFAULT 0,"
            " step INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (chat_id, issue_id))"
        )
        try:
            db.execute("ALTER TABLE chat_rules ADD COLUMN project_window_sec INTEGER")
        except sqlite3.OperationalError:
            pass
        # per (chat, project) last send — drives the "one message per project" window
        db.execute(
            "CREATE TABLE IF NOT EXISTS chat_project_state ("
            " chat_id TEXT NOT NULL, project TEXT NOT NULL,"
            " last_sent REAL NOT NULL DEFAULT 0, PRIMARY KEY (chat_id, project))"
        )
        db.commit()
