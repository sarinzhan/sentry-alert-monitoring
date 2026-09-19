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
        for stmt in (
            # the sentry-side slug, auto-filled from the API (read-only in the
            # web UI; `name` stays the user-editable display name)
            "ALTER TABLE project ADD COLUMN slug TEXT",
            # LLM observability audit («Проверить логи» in the web UI): does
            # this service ship enough events/logs to investigate complaints?
            "ALTER TABLE project ADD COLUMN audit TEXT",
            "ALTER TABLE project ADD COLUMN audit_at REAL",
            "ALTER TABLE project ADD COLUMN audit_llm_id TEXT",
        ):
            try:
                db.execute(stmt)
            except sqlite3.OperationalError:
                pass

        # web investigation history: every /api/explain run — the form fields,
        # the final answer, token usage and the llm_call audit id. Shown in the
        # web UI («История»).
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS web_request (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                at          REAL NOT NULL,
                description TEXT,
                request_id  TEXT,
                device_id   TEXT,
                msisdn      TEXT,
                period      TEXT,
                date_from   TEXT,
                date_to     TEXT,
                environment TEXT,
                llm_id      TEXT,
                response    TEXT,
                in_tokens   INTEGER,
                out_tokens  INTEGER,
                cost_usd    REAL,
                duration_ms INTEGER,
                error       TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS ix_web_request_at ON web_request(at)")
        for stmt in (
            # who ran the analysis (auth_user.username at the time of the run)
            "ALTER TABLE web_request ADD COLUMN username TEXT",
            # 1 = ran on the user's personal Claude token (doesn't count
            # against the shared-token daily quota, LLM_DAILY_LIMIT)
            "ALTER TABLE web_request ADD COLUMN own_token INTEGER DEFAULT 0",
        ):
            try:
                db.execute(stmt)
            except sqlite3.OperationalError:
                pass

        # web UI accounts (login form). role: admin | user. Passwords are kept
        # as entered — the admin screen shows and edits them (internal tool).
        # Seeded with admin/admin on first run (repositories.users.UsersRepo).
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_user (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE,
                password      TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'user',
                created       REAL NOT NULL,
                last_activity REAL
            )
            """
        )
        for stmt in (
            # personal Claude token (OAuth or API key) — used for the user's
            # analyses once their daily shared-token quota is spent
            "ALTER TABLE auth_user ADD COLUMN api_token TEXT",
            # 1 = run analyses on the personal token right away, without
            # waiting for the shared-token quota to run out (user's choice,
            # toggled in the web UI token panel)
            "ALTER TABLE auth_user ADD COLUMN use_own_token INTEGER DEFAULT 0",
        ):
            try:
                db.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # small key-value store: session-signing secret + runtime settings the
        # admin edits in the web UI (system prompt, daily LLM limits)
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

        # the LLM's persistent notes memory: navigation hints it saves at the
        # end of investigations (save_note) and searches at the start of the
        # next one (search_notes). One row per topic — saving an existing topic
        # updates it. embedding = float32 vector (services.embeddings) for
        # semantic search; NULL when saved while the model was unavailable.
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                topic     TEXT NOT NULL UNIQUE COLLATE NOCASE,
                content   TEXT NOT NULL,
                source    TEXT,
                embedding BLOB,
                emb_model TEXT,
                created   REAL NOT NULL,
                updated   REAL NOT NULL
            )
            """
        )

        # prepared prompts for the web investigation form. kind:
        #   role    — who the LLM answer is written for (client / tester /
        #             support / backend dev); text replaces the answer-style
        #             section of the explain prompt
        #   problem — common complaint templates; text prefills the
        #             description field
        # Seeded with defaults on first run (repositories.prompts.PromptsRepo),
        # then edited by the admin in the web UI.
        db.execute(
            "CREATE TABLE IF NOT EXISTS prompt_preset ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,"
            " name TEXT NOT NULL, text TEXT NOT NULL, updated REAL NOT NULL)"
        )

        # web chat («Вопрос LLM» tab): one conversation = one SDK session that
        # is resumed on every follow-up message, so the model keeps the full
        # context (its own earlier tool calls included). Messages are stored
        # only for rendering the chat — the model's memory is the session
        # transcript under $HOME/.claude (the persistent volume).
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_conversation (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT NOT NULL,
                session_id TEXT,
                title      TEXT,
                created    REAL NOT NULL,
                updated    REAL NOT NULL
            )
            """
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS chat_message ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id INTEGER NOT NULL,"
            " role TEXT NOT NULL, text TEXT NOT NULL, llm_id TEXT, at REAL NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS ix_chat_message ON chat_message(conv_id, at)")

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
