"""
SentryEventHandler — everything specific to Sentry webhooks.

Responsibilities:
  - verify the webhook signature
  - normalize the different Sentry payload shapes
  - debounce per issue (SQLite-backed, survives restarts)
  - format the Telegram message (+ optional LLM cause/fix)
  - send via an injected sender (ChatBotHandler.send)
"""
import ssl
import time
import json
import hmac
import asyncio
import hashlib
import sqlite3
import urllib.parse

import httpx

from config import (
    CLIENT_SECRET, DB_PATH, STAT_WINDOWS,
    ONGOING_INTERVAL_SEC, CRITICAL_WINDOW_SEC, CRITICAL_RATELIMIT_SEC,
    CRITICAL_ERROR_THRESHOLD, AFFECTED_USER_THRESHOLD,
    MUTE_MAX_DAYS, PROJECT_MUTE_MAX_DAYS, KEYWORD_MIN_INTERVAL_SEC,
    ENABLE_LLM, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_MAX_TOKENS,
    ANTHROPIC_PRICE_IN, ANTHROPIC_PRICE_OUT,
    ANTHROPIC_SSL_INSECURE, ANTHROPIC_CA_BUNDLE,
    LLM_STACK_LIB_MAX, LOG_RAW_PAYLOAD, LOG_LLM_PROMPT,
    SENTRY_API_URL, SENTRY_ORG, SENTRY_API_TOKEN, PROJECT_NAMES,
    GITLAB_URL, GITLAB_TOKEN, GITLAB_REF, GITLAB_CONTEXT_LINES, GITLAB_PROJECTS, log,
)
from utils import esc

# status -> emoji for the message header
STATUS_EMOJI = {"new": "🆕", "ongoing": "🔁", "escalating": "🚨"}


def _win_label(sec):
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec % n == 0:
            return f"{sec // n}{unit}"
    return f"{sec}s"


# e.g. "24h/12h/10m" — the period labels shown next to the line-2 counts
STAT_LABELS = "/".join(_win_label(w) for w in STAT_WINDOWS)


# Sentry issue lifecycle actions we treat as "an error is happening".
# None covers alert-rule / error payloads that have no 'action' field.
NOTIFY_ACTIONS = {None, "created", "triggered"}
LEVEL_EMOJI = {"fatal": "💀", "error": "🔴", "warning": "🟡", "info": "🔵", "debug": "⚪"}


def _first(*vals):
    for v in vals:
        if v:
            return v
    return None


class SentryEventHandler:
    # prune events older than the widest window we ever query
    PRUNE_HORIZON = max(max(STAT_WINDOWS), CRITICAL_WINDOW_SEC)

    def __init__(self, send, client, db_path=DB_PATH):
        self._send = send          # async (text, chat_id=, message_thread_id=) -> Message|None
        self._client = client      # httpx.AsyncClient, used for the LLM call
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
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
        # one row per received event, so we can count occurrences (and distinct users)
        # per issue over the stat/critical windows. Pruned to PRUNE_HORIZON.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS event_log (issue_id TEXT NOT NULL, ts REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_event_log ON event_log (issue_id, ts)"
        )
        # --- migrations (additive; ignore 'duplicate column' on re-run) ---
        for stmt in (
            "ALTER TABLE issue_state ADD COLUMN last_critical REAL DEFAULT 0",
            "ALTER TABLE issue_state ADD COLUMN short TEXT",
            "ALTER TABLE issue_state ADD COLUMN url TEXT",
            "ALTER TABLE issue_state ADD COLUMN project TEXT",
            "ALTER TABLE event_log ADD COLUMN usr TEXT",
        ):
            try:
                self._db.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # backfill the stable short id for pre-existing issues
        for (iid,) in self._db.execute(
            "SELECT issue_id FROM issue_state WHERE short IS NULL"
        ).fetchall():
            self._db.execute("UPDATE issue_state SET short=? WHERE issue_id=?",
                             (self._short(iid), iid))
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_issue_short ON issue_state(short)")
        # commands / mutes / keywords (all persisted -> survive restart)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS sent_message ("
            " message_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,"
            " issue_id TEXT NOT NULL, short TEXT, at REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS issue_mute ("
            " issue_id TEXT PRIMARY KEY, until REAL NOT NULL, by TEXT, at REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS project_mute ("
            " project TEXT PRIMARY KEY, until REAL NOT NULL, by TEXT, at REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS keyword ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,"
            " project TEXT, by TEXT, at REAL NOT NULL)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_keyword_proj ON keyword(project)")
        # vcs author name -> telegram handle (to @mention the culprit in the alert)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS user_map ("
            " vcs TEXT PRIMARY KEY, telegram TEXT NOT NULL, by TEXT, at REAL NOT NULL)"
        )
        # cached LLM analysis per issue, keyed also by the crash-line commit so it
        # invalidates when the code changes. Persisted so it survives restarts.
        self._db.execute(
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
        try:                                                  # migrate older DBs
            self._db.execute("ALTER TABLE analysis_cache ADD COLUMN cost REAL")
        except sqlite3.OperationalError:
            pass
        self._db.commit()
        self._lock = asyncio.Lock()
        self._prompt_logged = True   # log the LLM prompt once, so you can inspect it

        # project id -> name cache (error webhooks only carry the numeric id)
        self._proj_cache: dict[str, str] = {}
        self._proj_fetched = 0.0
        self._proj_lock = asyncio.Lock()
        # dedicated client: trust_env=False so it ignores HTTP(S)_PROXY and talks
        # to Sentry directly on the docker network.
        self._api = None
        if SENTRY_API_TOKEN:
            self._api = httpx.AsyncClient(
                base_url=SENTRY_API_URL,
                trust_env=False,
                timeout=10,
                headers={"Authorization": f"Bearer {SENTRY_API_TOKEN}"},
            )
        # GitLab client for source lookup (internal -> bypass the proxy, trust_env=False)
        self._path_cache: dict[str, str] = {}   # (repo, module) -> resolved repo path
        self._gitlab = None
        if GITLAB_URL and GITLAB_TOKEN and GITLAB_PROJECTS:
            self._gitlab = httpx.AsyncClient(
                base_url=GITLAB_URL,
                trust_env=False,
                timeout=10,
                headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
            )
        # Anthropic client — external, so it keeps trust_env (proxy) but needs the
        # same MITM-tolerant TLS as Telegram.
        self._llm = None
        if ENABLE_LLM and ANTHROPIC_API_KEY:
            ctx = ssl.create_default_context()
            if ANTHROPIC_CA_BUNDLE:
                ctx.load_verify_locations(ANTHROPIC_CA_BUNDLE)
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            if ANTHROPIC_SSL_INSECURE:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._llm = httpx.AsyncClient(timeout=30, verify=ctx)

    @staticmethod
    def _short(issue_id) -> str:
        """Stable short id (shown in the message, used by /mute and /status)."""
        return hashlib.sha1(str(issue_id).encode()).hexdigest()[:6]

    # -------------------------------------------------- counting + send decision
    async def register_and_decide(self, issue_id, title, usr=None, muted=False, forced=False):
        """
        Record this occurrence, then decide whether to send and with what status.
        Returns (send, status, counts over STAT_WINDOWS, short). Under a lock so
        concurrent webhooks for the same issue can't race the state.

        status: new | ongoing (>=12h since last) | escalating (critical spike/users)
        muted:  suppress unless forced by a keyword.  forced: keyword force-send.
        """
        now = time.time()
        async with self._lock:
            self._db.execute("INSERT INTO event_log (issue_id, ts, usr) VALUES (?, ?, ?)",
                             (issue_id, now, usr))
            self._db.execute("DELETE FROM event_log WHERE ts < ?", (now - self.PRUNE_HORIZON,))

            counts = [
                self._db.execute(
                    "SELECT COUNT(*) FROM event_log WHERE issue_id=? AND ts>=?", (issue_id, now - w)
                ).fetchone()[0]
                for w in STAT_WINDOWS
            ]

            row = self._db.execute(
                "SELECT last_sent, step, short, last_critical FROM issue_state WHERE issue_id=?",
                (issue_id,),
            ).fetchone()

            if row is None:                                   # first time we see this issue
                short = self._short(issue_id)
                if muted and not forced:                      # remember it, but don't send
                    self._db.execute(
                        "INSERT INTO issue_state(issue_id, last_sent, step, last_critical, short, title, updated) "
                        "VALUES (?, 0, 0, 0, ?, ?, ?)", (issue_id, short, title, now))
                    self._db.commit()
                    return False, None, counts, short
                self._db.execute(
                    "INSERT INTO issue_state(issue_id, last_sent, step, last_critical, short, title, updated) "
                    "VALUES (?, ?, 1, 0, ?, ?, ?)", (issue_id, now, short, title, now))
                self._db.commit()
                return True, "new", counts, short

            last_sent, step, short, last_critical = row
            if muted and not forced:
                self._db.commit()
                return False, None, counts, short

            since = now - CRITICAL_WINDOW_SEC
            crit_count = self._db.execute(
                "SELECT COUNT(*) FROM event_log WHERE issue_id=? AND ts>=?", (issue_id, since)
            ).fetchone()[0]
            crit_users = self._db.execute(
                "SELECT COUNT(DISTINCT usr) FROM event_log WHERE issue_id=? AND ts>=? AND usr IS NOT NULL",
                (issue_id, since),
            ).fetchone()[0]
            is_critical = (crit_count > CRITICAL_ERROR_THRESHOLD) or (crit_users >= AFFECTED_USER_THRESHOLD)
            critical_allowed = is_critical and (now - (last_critical or 0)) >= CRITICAL_RATELIMIT_SEC
            ongoing_ok = (now - last_sent) >= ONGOING_INTERVAL_SEC

            if critical_allowed:
                status = "escalating"
            elif ongoing_ok:
                status = "ongoing"
            else:
                status = None

            if forced and status is None:                     # keyword force-send
                if KEYWORD_MIN_INTERVAL_SEC and (now - last_sent) < KEYWORD_MIN_INTERVAL_SEC:
                    self._db.commit()
                    return False, None, counts, short
                status = "ongoing"

            if status is None:
                self._db.commit()
                return False, None, counts, short

            self._db.execute(
                "UPDATE issue_state SET last_sent=?, step=?, last_critical=?, title=?, updated=? "
                "WHERE issue_id=?",
                (now, step + 1, now if status == "escalating" else (last_critical or 0),
                 title, now, issue_id),
            )
            self._db.commit()
            return True, status, counts, short

    # ------------------------------------------------------ commands data layer
    def store_sent(self, message_id, chat_id, issue_id, short, url, project):
        """Map a sent alert -> issue (for reply-based /mute) and cache url/project."""
        now = time.time()
        self._db.execute(
            "INSERT OR REPLACE INTO sent_message(message_id, chat_id, issue_id, short, at) "
            "VALUES (?, ?, ?, ?, ?)", (message_id, chat_id, issue_id, short, now))
        self._db.execute("DELETE FROM sent_message WHERE at < ?", (now - 30 * 86400,))
        self._db.execute("UPDATE issue_state SET url=?, project=? WHERE issue_id=?",
                         (url, project, issue_id))
        self._db.commit()

    def resolve_ref(self, ref):
        """Look up an issue by its short id (#abc123) or raw issue id."""
        ref = (ref or "").strip().lstrip("#")
        if not ref:
            return None
        row = self._db.execute(
            "SELECT issue_id, short, title, project, url FROM issue_state "
            "WHERE short=? OR issue_id=? LIMIT 1", (ref.lower(), ref)).fetchone()
        if not row:
            return None
        return {"issue_id": row[0], "short": row[1], "title": row[2],
                "project": row[3], "url": row[4]}

    def resolve_reply(self, message_id):
        """Find the issue behind an alert message the user replied to."""
        row = self._db.execute(
            "SELECT s.issue_id, s.short, i.title, i.project, i.url FROM sent_message s "
            "LEFT JOIN issue_state i ON i.issue_id=s.issue_id WHERE s.message_id=?",
            (message_id,)).fetchone()
        if not row:
            return None
        return {"issue_id": row[0], "short": row[1], "title": row[2],
                "project": row[3], "url": row[4]}

    def issue_status(self, ref):
        info = self.resolve_ref(ref)
        if not info:
            return None
        iid, now = info["issue_id"], time.time()
        info["counts"] = [self._db.execute(
            "SELECT COUNT(*) FROM event_log WHERE issue_id=? AND ts>=?", (iid, now - w)
        ).fetchone()[0] for w in STAT_WINDOWS]
        st = self._db.execute(
            "SELECT last_sent, last_critical FROM issue_state WHERE issue_id=?", (iid,)
        ).fetchone() or (0, 0)
        info["last_sent"], info["last_critical"] = st
        info["muted_until"] = self.is_issue_muted(iid, now)
        return info

    @staticmethod
    def _clamp_days(days, maximum):
        try:
            return max(1, min(int(days), maximum))
        except (TypeError, ValueError):
            return None

    def mute_issue(self, ref, days, by):
        info = self.resolve_ref(ref)
        if not info:
            return None
        d = self._clamp_days(days, MUTE_MAX_DAYS)
        if d is None:
            return None
        now = time.time()
        info["days"], info["until"] = d, now + d * 86400
        self._db.execute(
            "INSERT OR REPLACE INTO issue_mute(issue_id, until, by, at) VALUES (?, ?, ?, ?)",
            (info["issue_id"], info["until"], by, now))
        self._db.commit()
        return info

    def mute_project(self, project_ref, days, by):
        d = self._clamp_days(days, PROJECT_MUTE_MAX_DAYS)
        if d is None:
            return None
        now, key = time.time(), str(project_ref).strip()
        until = now + d * 86400
        self._db.execute(
            "INSERT OR REPLACE INTO project_mute(project, until, by, at) VALUES (?, ?, ?, ?)",
            (key, until, by, now))
        self._db.commit()
        return {"project": key, "days": d, "until": until}

    def is_issue_muted(self, issue_id, now=None):
        now = now or time.time()
        row = self._db.execute("SELECT until FROM issue_mute WHERE issue_id=?", (issue_id,)).fetchone()
        return row[0] if row and row[0] > now else None

    def is_project_muted(self, p, now=None):
        now = now or time.time()
        for k in (str(p.get("project_id")), p.get("project")):
            if not k:
                continue
            row = self._db.execute("SELECT until FROM project_mute WHERE project=?", (k,)).fetchone()
            if row and row[0] > now:
                return row[0]
        return None

    def is_suppressed(self, issue_id, p):
        return bool(self.is_issue_muted(issue_id) or self.is_project_muted(p))

    def keyword_match(self, text, p):
        """Return the first watched keyword contained in text (global or this project)."""
        t = (text or "").lower()
        if not t:
            return None
        projkeys = [k for k in (str(p.get("project_id")), p.get("project")) if k]
        for kw, proj in self._db.execute("SELECT text, project FROM keyword").fetchall():
            if kw and kw in t and (proj is None or proj in projkeys):
                return kw
        return None

    def add_keyword(self, text, project, by):
        text = (text or "").strip().lower()
        if not text:
            return False
        project = str(project).strip() if project else None
        if self._db.execute("SELECT 1 FROM keyword WHERE text=? AND (project IS ? OR project=?)",
                            (text, project, project)).fetchone():
            return False
        self._db.execute("INSERT INTO keyword(text, project, by, at) VALUES (?, ?, ?, ?)",
                         (text, project, by, time.time()))
        self._db.commit()
        return True

    def del_keyword(self, text, project):
        text = (text or "").strip().lower()
        project = str(project).strip() if project else None
        cur = self._db.execute("DELETE FROM keyword WHERE text=? AND (project IS ? OR project=?)",
                               (text, project, project))
        self._db.commit()
        return cur.rowcount

    def list_keywords(self):
        return self._db.execute(
            "SELECT text, project FROM keyword ORDER BY project IS NULL DESC, project, text"
        ).fetchall()

    # --- muted issues / projects (list + unmute) ---
    def list_muted_issues(self, now=None):
        now = now or time.time()
        return self._db.execute(
            "SELECT m.issue_id, i.short, i.title, m.until, m.by FROM issue_mute m "
            "LEFT JOIN issue_state i ON i.issue_id=m.issue_id "
            "WHERE m.until > ? ORDER BY m.until", (now,)).fetchall()

    def unmute_issue(self, ref):
        info = self.resolve_ref(ref)
        if not info:
            return None
        self._db.execute("DELETE FROM issue_mute WHERE issue_id=?", (info["issue_id"],))
        self._db.commit()
        return info

    def list_project_mutes(self, now=None):
        now = now or time.time()
        return self._db.execute(
            "SELECT project, until, by FROM project_mute WHERE until > ? ORDER BY until", (now,)
        ).fetchall()

    def unmute_project(self, project):
        cur = self._db.execute("DELETE FROM project_mute WHERE project=?", (str(project).strip(),))
        self._db.commit()
        return cur.rowcount

    def projects_overview(self, now=None):
        """Known projects (from config) + their mute state."""
        muted = {row[0]: row[1] for row in self.list_project_mutes(now)}
        ids = set(PROJECT_NAMES) | set(GITLAB_PROJECTS) | set(muted)
        return [{"id": pid, "name": PROJECT_NAMES.get(pid), "repo": GITLAB_PROJECTS.get(pid),
                 "muted_until": muted.get(pid)} for pid in sorted(ids, key=str)]

    # --- vcs author -> telegram handle ---
    def map_add(self, vcs, telegram, by):
        vcs = (vcs or "").strip().lower()
        telegram = (telegram or "").strip().lstrip("@")
        if not vcs or not telegram:
            return False
        self._db.execute("INSERT OR REPLACE INTO user_map(vcs, telegram, by, at) VALUES (?, ?, ?, ?)",
                         (vcs, telegram, by, time.time()))
        self._db.commit()
        return True

    def map_del(self, vcs):
        cur = self._db.execute("DELETE FROM user_map WHERE vcs=?", ((vcs or "").strip().lower(),))
        self._db.commit()
        return cur.rowcount

    def map_list(self):
        return self._db.execute("SELECT vcs, telegram FROM user_map ORDER BY vcs").fetchall()

    def map_lookup(self, author):
        if not author:
            return None
        row = self._db.execute("SELECT telegram FROM user_map WHERE vcs=?",
                               (author.strip().lower(),)).fetchone()
        return row[0] if row else None

    async def aclose(self):
        if self._api is not None:
            await self._api.aclose()
        if self._gitlab is not None:
            await self._gitlab.aclose()
        if self._llm is not None:
            await self._llm.aclose()

    # ---------------------------------------------------------- project names
    async def resolve_project(self, project):
        """Map a project id to a name. Order: static map -> API -> raw id fallback."""
        if project is None:
            return None
        s = str(project)
        if not s.isdigit():
            return s                       # already a slug/name (issue payloads)
        if s in PROJECT_NAMES:             # static override, no network needed
            return PROJECT_NAMES[s]
        if self._api is not None:
            if s not in self._proj_cache:
                await self._refresh_projects()
            if s in self._proj_cache:
                return self._proj_cache[s]
        return s                           # fallback: show the numeric id (mappable)

    async def _refresh_projects(self):
        async with self._proj_lock:
            now = time.time()
            if now - self._proj_fetched < 60:      # throttle repeated misses
                return
            self._proj_fetched = now
            try:
                r = await self._api.get(
                    f"/api/0/organizations/{SENTRY_ORG}/projects/",
                    params={"per_page": 100},
                )
                r.raise_for_status()
                for proj in r.json():
                    self._proj_cache[str(proj["id"])] = proj.get("slug") or proj.get("name")
                log.info("project cache refreshed: %d projects", len(self._proj_cache))
            except httpx.HTTPStatusError as e:
                # surface the real reason (e.g. 403 = token missing project:read)
                log.warning("project name fetch failed: %s %s",
                            e.response.status_code, e.response.text[:200])
            except Exception as e:
                log.warning("project name fetch failed: %s", e)

    # ------------------------------------------------------------- security
    @staticmethod
    def verify(body: bytes, sig) -> bool:
        if not CLIENT_SECRET:
            return True
        if not sig:
            log.warning("verify: no sentry-hook-signature header")
            return False
        expected = hmac.new(CLIENT_SECRET.encode(), body, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(expected, sig)
        if not ok:
            # Sentry sends a signature, not the secret. If these differ, either our
            # secret is wrong or the body doesn't match what Sentry signed.
            log.warning(
                "verify FAIL: received=%s expected=%s (secret_len=%d head=%s tail=%s, body_bytes=%d)",
                sig, expected, len(CLIENT_SECRET), CLIENT_SECRET[:4], CLIENT_SECRET[-4:], len(body),
            )
        return ok

    # ------------------------------------------------------------- parsing
    @staticmethod
    def parse(resource: str, payload: dict):
        """
        Normalize the different Sentry webhook shapes (issue / error / event_alert)
        into one flat dict. Returns None if there is nothing useful to send.
        """
        data = payload.get("data", {}) or {}
        action = payload.get("action")

        obj = data.get("issue") or data.get("error") or data.get("event") or {}
        if not obj:
            return None

        # stable issue id used as the debounce key
        issue_id = str(
            _first(
                obj.get("id") if resource == "issue" else None,
                obj.get("issue_id"),
                obj.get("groupID"),
                obj.get("group_id"),
                obj.get("id"),
            )
            or ""
        )
        if not issue_id:
            return None

        metadata = obj.get("metadata") or {}
        exc_type = metadata.get("type")
        exc_value = metadata.get("value")

        # event payloads carry the stacktrace; issue payloads usually don't
        # exc_chain: every exception in the chain (Caused by ...), most recent last
        exc_chain = []
        frames_struct = []                                # richer, for LLM + GitLab
        values = (obj.get("exception") or {}).get("values") or []
        for v in values:
            vt, vv = v.get("type"), v.get("value")
            if vt or vv:
                exc_chain.append((vt, str(vv)[:500] if vv else vv))
        if values:
            last = values[-1]                             # the thrown exception
            exc_type = exc_type or last.get("type")
            exc_value = exc_value or last.get("value")
            st = (last.get("stacktrace") or {}).get("frames") or []
            for f in reversed(st):                        # crash site first
                frames_struct.append({
                    "function": f.get("function") or "?",
                    "filename": f.get("filename"),
                    "module": f.get("module"),
                    "abs_path": _first(f.get("absPath"), f.get("abs_path")),
                    "lineno": f.get("lineno"),
                    "in_app": bool(f.get("inApp") if "inApp" in f else f.get("in_app")),
                    "context_line": _first(f.get("context_line"), f.get("contextLine")),
                })

        def _fmt(e):
            where = e["filename"] or e["module"] or "?"
            tag = "" if e["in_app"] else " (lib)"
            base = f"{where}:{e['lineno']}" if e["lineno"] else where
            return f"{base} in {e['function']}{tag}"

        frames = [_fmt(e) for e in frames_struct[:5]]                 # short, for Telegram
        # for the LLM: the full in-app (project) trace, plus only the top few
        # library frames for crash-site context (lib frames are mostly noise)
        frames_full, lib_seen = [], 0
        for e in frames_struct:                                       # crash-site first
            if e["in_app"]:
                frames_full.append(_fmt(e))
            elif lib_seen < LLM_STACK_LIB_MAX:
                frames_full.append(_fmt(e))
                lib_seen += 1
        frames_full = frames_full[:60]                                # hard safety cap

        if exc_value:
            exc_value = str(exc_value)[:1000]

        project = obj.get("project")
        # keep the raw numeric project id for GitLab repo mapping (before name resolution)
        project_id = project.get("id") if isinstance(project, dict) else project
        if isinstance(project, dict):
            project = project.get("slug") or project.get("name")

        # affected-user identifier for the "critical by users" rule
        u = obj.get("user") or {}
        usr = _first(u.get("id"), u.get("email"), u.get("username"), u.get("ip_address"))
        if not usr:
            for t in (obj.get("tags") or []):
                if isinstance(t, (list, tuple)) and len(t) == 2 and t[0] == "user":
                    usr = t[1]; break
                if isinstance(t, dict) and t.get("key") == "user":
                    usr = t.get("value"); break
        usr = str(usr)[:200] if usr else None

        return {
            "issue_id": issue_id,
            "usr": usr,
            "event_id": _first(obj.get("event_id"), obj.get("eventID")),
            "action": action,
            "title": obj.get("title") or exc_type or "Sentry event",
            "culprit": obj.get("culprit"),
            "level": obj.get("level"),
            "environment": obj.get("environment"),
            "type": exc_type,
            "value": exc_value,
            "count": obj.get("count"),
            "user_count": _first(obj.get("userCount"), obj.get("user_count")),
            "url": _first(obj.get("permalink"), obj.get("web_url"), obj.get("url")),
            "frames": frames,
            "frames_full": frames_full,
            "frames_struct": frames_struct,
            "exc_chain": exc_chain,
            "project": project,
            "project_id": project_id,
        }

    # ------------------------------------------------------------- format
    @staticmethod
    def build_message(p: dict, analysis: str = None) -> str:
        status = p.get("status") or "ongoing"
        emoji = STATUS_EMOJI.get(status, "🔴")
        project = esc(p.get("project") or "sentry")
        env = esc(p.get("environment") or "?")

        # line 1: <emoji> project · env · status
        lines = [f"{emoji} <b>{project}</b> · {env} · {esc(status)}"]
        # line 2: <@telegram or vcs author> · <commit date-time> · counts (periods) · #<short>
        nums = "/".join(esc(c) for c in (p.get("counts") or []))
        counts = f"{nums} ({STAT_LABELS})" if nums else ""
        b = p.get("blame") or {}
        who = f"@{esc(b['tg'])}" if b.get("tg") else (esc(b["author"]) if b.get("author") else "")
        author = f"{who} · {esc(b.get('date') or '?')}" if who else ""
        short = f"<code>#{esc(p.get('short'))}</code>" if p.get("short") else ""
        lines.append(" · ".join(x for x in (author, counts, short) if x))
        lines.append("")

        # LLM cause / fix first (only present for escalating prod alerts)
        if analysis:
            lines.append(analysis)
            lines.append("")

        # then the rest
        lines.append(f"<b>{esc(p.get('title'))}</b>")
        if p.get("value") and p.get("value") != p.get("title"):
            lines.append(f"<code>{esc(p['value'])}</code>")
        lines.append("")

        if p.get("culprit"):
            lines.append(f"<b>Culprit:</b> <code>{esc(p['culprit'])}</code>")

        meta = []
        if p.get("level"):      meta.append(f"level {esc(p['level'])}")
        if p.get("count"):      meta.append(f"events {esc(p['count'])}")
        if p.get("user_count"): meta.append(f"users {esc(p['user_count'])}")
        if meta:
            lines.append(" · ".join(meta))

        if p.get("frames"):
            lines.append("<pre>" + "\n".join(esc(f) for f in p["frames"]) + "</pre>")

        if p.get("url"):
            lines += ["", f'<a href="{esc(p["url"])}">Open in Sentry →</a>']

        # copyable quick commands (tap to copy on mobile)
        if p.get("short"):
            s = esc(p["short"])
            lines.append(f"<code>/status {s}</code>   <code>/mute {s} 1</code>")

        # bottom: LLM API cost, or "cached" (with what it saved) on a cache hit
        m = p.get("llm_meta")
        if m:
            if m.get("cached"):
                saved = f" (saved ~${m['cost']:.4f})" if m.get("cost") else ""
                lines.append(f"<i>💰 LLM: cached{esc(saved)}</i>")
            else:
                lines.append(
                    f"<i>💰 LLM: ${m.get('cost', 0):.4f} · "
                    f"{esc(m.get('in', 0))} in / {esc(m.get('out', 0))} out</i>"
                )

        return "\n".join(lines)

    # ------------------------------------------------------------- gitlab source
    @staticmethod
    def _pick_frame(p: dict):
        """Frame to fetch source for: topmost in-app frame with a line number.
        Only in-app frames — library files (Spring, etc.) aren't in the repo."""
        frames = p.get("frames_struct") or []
        return next((f for f in frames if f["in_app"] and f.get("lineno")), None)

    @staticmethod
    def _candidate_paths(frame: dict):
        """Guess repo-relative paths for a (Java) frame, most specific first."""
        module = frame.get("module") or ""
        filename = frame.get("filename") or ""
        paths = []
        if frame.get("abs_path") and "/" in frame["abs_path"]:
            paths.append(frame["abs_path"].lstrip("/"))
        if module and "." in module:
            pkg = module.rsplit(".", 1)[0].replace(".", "/")
            fname = filename or (module.rsplit(".", 1)[1] + ".java")
            paths.append(f"src/main/java/{pkg}/{fname}")
            paths.append(f"src/main/kotlin/{pkg}/{fname}")
            paths.append(f"{pkg}/{fname}")
        elif filename:
            paths.append(filename)
        # de-dup, preserve order
        seen, out = set(), []
        for p in paths:
            if p not in seen:
                seen.add(p); out.append(p)
        return out

    async def locate_source(self, p: dict):
        """Resolve (proj_enc, path, lineno) for the crash frame, or None. Cached per file."""
        if self._gitlab is None:
            return None
        repo = GITLAB_PROJECTS.get(str(p.get("project_id")))
        if not repo:
            return None
        frame = self._pick_frame(p)
        if not frame:
            return None
        proj_enc = urllib.parse.quote(str(repo), safe="")

        ckey = f"{repo}::{frame.get('module')}"
        path = self._path_cache.get(ckey)
        if path is None:
            # verify the project resolves first, so we can tell "wrong project" from
            # "wrong path". GITLAB_PROJECTS must be a numeric id or full namespace path.
            try:
                pr = await self._gitlab.get(f"/api/v4/projects/{proj_enc}")
            except Exception as e:
                log.warning("gitlab project error repo=%s: %s", repo, e)
                return None
            if pr.status_code != 200:
                log.warning("gitlab project NOT FOUND repo=%s (%s) — GITLAB_PROJECTS needs a "
                            "numeric project id or the full namespace path (e.g. mobile/billing)",
                            repo, pr.status_code)
                return None
            path = await self._resolve_path(proj_enc, frame)
            if not path:
                log.warning("gitlab: file not found repo=%s file=%s tried=%s",
                            repo, frame.get("filename"), self._candidate_paths(frame))
                return None
            self._path_cache[ckey] = path
        return proj_enc, path, int(frame["lineno"])

    async def fetch_source(self, loc):
        """Source window around the crash line (loc from locate_source)."""
        proj_enc, path, lineno = loc
        return await self._read_file(proj_enc, path, lineno)

    async def fetch_blame(self, loc):
        """Who last changed the crash line — GitLab blame for that single line."""
        proj_enc, path, lineno = loc
        enc = urllib.parse.quote(path, safe="")
        try:
            r = await self._gitlab.get(
                f"/api/v4/projects/{proj_enc}/repository/files/{enc}/blame",
                params={"ref": GITLAB_REF, "range[start]": lineno, "range[end]": lineno},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("gitlab blame error path=%s: %s", path, e)
            return None
        commit = ((data[0] if data else {}) or {}).get("commit") or {}
        if not commit:
            return None
        sha = (commit.get("id") or "")[:8]
        msg = (commit.get("message") or "").strip()
        log.info("gitlab blame path=%s line=%d author=%s commit=%s",
                 path, lineno, commit.get("author_name"), sha)
        return {
            "author": commit.get("author_name"),
            "email": commit.get("author_email"),
            "sha": sha,
            "sha_full": commit.get("id"),
            "subject": (msg.splitlines()[0] if msg else "")[:80],
            # "2025-07-18T15:52:33+06:00" -> "2025-07-18 15:52"
            "date": (commit.get("committed_date") or "")[:16].replace("T", " "),
            "line": lineno,
        }

    async def fetch_change(self, loc, sha_full):
        """The diff of the file in the commit that last touched the crash line
        (previous state -> current state). Returns unified-diff text or None."""
        if not sha_full:
            return None
        proj_enc, path, _ = loc
        sha_enc = urllib.parse.quote(str(sha_full), safe="")
        try:
            r = await self._gitlab.get(
                f"/api/v4/projects/{proj_enc}/repository/commits/{sha_enc}/diff"
            )
            r.raise_for_status()
            diffs = r.json()
        except Exception as e:
            log.warning("gitlab diff error commit=%s: %s", str(sha_full)[:8], e)
            return None
        for d in diffs if isinstance(diffs, list) else []:
            if path in (d.get("new_path"), d.get("old_path")):
                text = (d.get("diff") or "").strip()
                if text:
                    log.info("gitlab change ok commit=%s path=%s", str(sha_full)[:8], path)
                    return text[:3000]
        return None

    async def _resolve_path(self, proj_enc: str, frame: dict):
        """Convention paths first; fall back to a repo filename search (multi-module)."""
        for path in self._candidate_paths(frame):
            enc = urllib.parse.quote(path, safe="")
            r = await self._gitlab.get(
                f"/api/v4/projects/{proj_enc}/repository/files/{enc}",
                params={"ref": GITLAB_REF},
            )
            if r.status_code == 200:
                return path
        # fallback: search the repo for the class, match by filename + package path
        module = frame.get("module") or ""
        filename = frame.get("filename") or ""
        classname = module.rsplit(".", 1)[-1] if module else filename.rsplit(".", 1)[0]
        pkgpath = module.rsplit(".", 1)[0].replace(".", "/") if "." in module else ""
        try:
            r = await self._gitlab.get(
                f"/api/v4/projects/{proj_enc}/search",
                params={"scope": "blobs", "search": classname, "ref": GITLAB_REF},
            )
            r.raise_for_status()
            hits = r.json()
        except Exception as e:
            log.warning("gitlab search error: %s", e)
            return None
        best = None
        for h in hits if isinstance(hits, list) else []:
            hp = h.get("path", "")
            if filename and not (hp.endswith("/" + filename) or hp == filename):
                continue
            if pkgpath and hp.endswith(f"{pkgpath}/{filename}"):
                log.info("gitlab: located via search -> %s", hp)
                return hp
            best = best or hp
        if best:
            log.info("gitlab: located via search (loose) -> %s", best)
        return best

    async def _read_file(self, proj_enc: str, path: str, lineno: int):
        lo, hi = max(1, lineno - GITLAB_CONTEXT_LINES), lineno + GITLAB_CONTEXT_LINES
        enc = urllib.parse.quote(path, safe="")
        try:
            r = await self._gitlab.get(
                f"/api/v4/projects/{proj_enc}/repository/files/{enc}/raw",
                params={"ref": GITLAB_REF},
            )
            r.raise_for_status()
        except Exception as e:
            log.warning("gitlab read error path=%s: %s", path, e)
            return None
        lines = r.text.splitlines()
        window = lines[lo - 1:hi]
        body = "\n".join(f"{i}{'>' if i == lineno else ':'} {t}"
                         for i, t in enumerate(window, start=lo))
        log.info("gitlab source ok path=%s line=%d", path, lineno)
        return f"{path} (ref {GITLAB_REF}), lines {lo}-{min(hi, len(lines))}:\n{body}"

    # ------------------------------------------------------------- LLM (future)
    async def analyze(self, p: dict):
        """
        Ask an LLM for a likely cause + suggested fix. Returns None unless the LLM
        is enabled. Cached per issue (keyed by the crash-line commit) so repeats of
        the same issue reuse the answer until the code changes.
        """
        issue_id = p.get("issue_id")
        blame_sha = (p.get("blame") or {}).get("sha_full") or "-"
        # cache hit (SQLite, survives restart) -> skip GitLab fetches, prompt, LLM call
        if self._llm is not None and issue_id:
            row = self._db.execute(
                "SELECT analysis, cost FROM analysis_cache WHERE issue_id=? AND blame_sha=?",
                (issue_id, blame_sha),
            ).fetchone()
            if row:
                log.info("llm cache hit issue=%s commit=%s", issue_id, blame_sha[:8])
                p["llm_meta"] = {"cached": True, "cost": row[1]}
                return row[0]

        # source window + the diff of the commit that last touched the crash line
        # ("previous -> current state"), both reusing the file located in process()
        loc = p.get("_loc")
        blame = p.get("blame") or {}
        source = await self.fetch_source(loc) if loc else None
        change = await self.fetch_change(loc, blame.get("sha_full")) \
            if (loc and blame.get("sha_full")) else None

        chain = "\n".join(f"{t}: {v}" for t, v in (p.get("exc_chain") or [])) \
            or f"{p.get('type')}: {p.get('value')}"
        stack = "\n".join(p.get("frames_full") or p.get("frames") or [])
        prompt = (
            "You are a senior backend engineer triaging a Sentry error. Use the recent "
            "change diff to judge whether it introduced the bug. "
            "Reply in at most 4 short lines, plain text:\n"
            "Likely cause: <one sentence>\n"
            "Suggested fix: <one or two sentences>\n\n"
            f"Culprit: {p.get('culprit')}\n"
            f"Environment: {p.get('environment')}\n"
            f"Exception chain (most recent last):\n{chain}\n\n"
            f"Stack (crash site first):\n{stack}"
        )
        if source:
            prompt += f"\n\nCurrent source around the crash site (from GitLab):\n{source}"
        if change:
            prompt += (
                f"\n\nMost recent change to this file "
                f"(commit {blame.get('sha')} by {blame.get('author')} on {blame.get('date')} — "
                f"\"{blame.get('subject')}\") — previous vs current (diff):\n{change}"
            )

        # dump the prompt so you can inspect it (even while ENABLE_LLM is off)
        log.info("LLM prompt preview:\n%s", prompt)

        if self._llm is None:
            return None

        try:
            r = await self._llm.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": ANTHROPIC_MAX_TOKENS,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            usage = data.get("usage") or {}
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            cost = in_tok / 1e6 * ANTHROPIC_PRICE_IN + out_tok / 1e6 * ANTHROPIC_PRICE_OUT
            text = "".join(
                b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
            ).strip()
            result = ("🤖 " + esc(text)) if text else None
            p["llm_meta"] = {"cached": False, "cost": cost, "in": in_tok, "out": out_tok}
            log.info("llm call issue=%s in=%d out=%d cost=$%.4f",
                     issue_id, in_tok, out_tok, cost)
            if result and issue_id:
                self._db.execute(
                    "INSERT OR REPLACE INTO analysis_cache(issue_id, blame_sha, analysis, cost, updated) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (issue_id, blame_sha, result, cost, time.time()),
                )
                self._db.commit()
            return result
        except Exception as e:
            log.warning("LLM analysis failed: %s", e)
            return None

    # ------------------------------------------------------------- debug
    @staticmethod
    def _log_raw(resource: str, payload: dict):
        """Dump the raw webhook so we can see the payload shape (esp. `project`)."""
        data = payload.get("data", {}) or {}
        obj = data.get("issue") or data.get("error") or data.get("event") or {}
        proj_fields = {k: obj.get(k) for k in obj if "project" in k.lower()}
        log.info("raw webhook resource=%s action=%s top_keys=%s obj_keys=%s",
                 resource, payload.get("action"), sorted(payload.keys()), sorted(obj.keys()))
        log.info("raw project* fields: %s", proj_fields)
        log.info("raw payload json (truncated 20k): %s",
                 json.dumps(payload, default=str)[:20000])

    # ------------------------------------------------------------- pipeline
    async def process(self, resource: str, payload: dict):
        try:
            if LOG_RAW_PAYLOAD:
                self._log_raw(resource, payload)
            p = self.parse(resource, payload)
            if not p:
                log.info("ignored: nothing to parse")
                return
            if p.get("action") not in NOTIFY_ACTIONS:
                log.info("ignored action=%s issue=%s", p.get("action"), p["issue_id"])
                return
            # keyword force-send bypasses debounce + mute; else respect mutes
            forced = self.keyword_match(f"{p.get('title') or ''} {p.get('value') or ''}", p) is not None
            muted = (not forced) and self.is_suppressed(p["issue_id"], p)
            # record the occurrence, get counts, and decide send + status atomically
            send, status, p["counts"], p["short"] = await self.register_and_decide(
                p["issue_id"], p.get("title") or "", p.get("usr"), muted, forced)
            if not send:
                log.info("skip issue=%s counts=%s muted=%s", p["issue_id"], p["counts"], muted)
                # debug: build + log the prompt for skipped events too
                if LOG_LLM_PROMPT:
                    p["_loc"] = await self.locate_source(p)
                    if p["_loc"]:
                        p["blame"] = await self.fetch_blame(p["_loc"])
                    await self.analyze(p)     # logs the prompt; returns None while LLM off
                return
            p["status"] = status
            # numeric project id -> name (None falls back to "sentry" in the header)
            p["project"] = await self.resolve_project(p.get("project"))
            # locate the crash file once -> reused for blame (author, shown always) + source
            p["_loc"] = await self.locate_source(p)
            if p["_loc"]:
                p["blame"] = await self.fetch_blame(p["_loc"])
                if p.get("blame"):                            # map vcs author -> @telegram
                    p["blame"]["tg"] = self.map_lookup(p["blame"].get("author"))
            # LLM cause/fix only for escalating alerts in prod
            is_prod = (p.get("environment") or "").lower() == "prod"
            analysis = await self.analyze(p) if (status == "escalating" and is_prod) else None
            msg = await self._send(self.build_message(p, analysis))
            if msg is not None:
                self.store_sent(msg.message_id, msg.chat_id, p["issue_id"],
                                p["short"], p.get("url"), p.get("project"))
            log.info("sent issue=%s status=%s counts=%s%s",
                     p["issue_id"], status, p["counts"], " FORCED" if forced else "")
        except Exception as e:
            log.exception("process failed: %s", e)
