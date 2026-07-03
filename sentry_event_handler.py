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
    CLIENT_SECRET, DB_PATH, SEND_WINDOWS, SPIKE_THRESHOLD,
    ENABLE_LLM, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_MAX_TOKENS,
    ANTHROPIC_SSL_INSECURE, ANTHROPIC_CA_BUNDLE,
    LLM_STACK_LIB_MAX, LOG_RAW_PAYLOAD, LOG_LLM_PROMPT,
    SENTRY_API_URL, SENTRY_ORG, SENTRY_API_TOKEN, PROJECT_NAMES,
    GITLAB_URL, GITLAB_TOKEN, GITLAB_REF, GITLAB_CONTEXT_LINES, GITLAB_PROJECTS, log,
)
from utils import esc

# status -> emoji for the message header
STATUS_EMOJI = {"new": "🆕", "ongoing": "🔁", "escalating": "🚨"}


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
    def __init__(self, send, client, db_path=DB_PATH, windows=SEND_WINDOWS):
        self._send = send          # async (text, chat_id=, message_thread_id=) -> bool
        self._client = client      # httpx.AsyncClient, used for the LLM call
        self._windows = windows
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
        # one row per received event, so we can count occurrences per issue
        # (total + last 5 min). Pruned to the last 24h to stay bounded.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS event_log (issue_id TEXT NOT NULL, ts REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_event_log ON event_log (issue_id, ts)"
        )
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
        # issue_id -> (blame_sha, analysis text): reuse the LLM answer until the
        # crash-line commit changes, so repeats of the same issue don't re-call the LLM
        self._analysis_cache: dict[str, tuple] = {}
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

    # -------------------------------------------------- counting + send decision
    async def register_and_decide(self, issue_id: str, title: str):
        """
        Record this occurrence, then decide whether to send and with what status.
        Returns (send: bool, status: str|None, c24h, c30m, c5m). Under a lock so
        concurrent webhooks for the same issue can't race the counts/state.

        status:
          new         first message ever for this issue
          escalating  >= SPIKE_THRESHOLD events since our last message for it
          ongoing     a normal debounce-window send
        """
        now = time.time()
        async with self._lock:
            self._db.execute("INSERT INTO event_log (issue_id, ts) VALUES (?, ?)", (issue_id, now))
            self._db.execute("DELETE FROM event_log WHERE ts < ?", (now - 86400,))  # prune >24h

            def count(since=None):
                if since is None:
                    return self._db.execute(
                        "SELECT COUNT(*) FROM event_log WHERE issue_id=?", (issue_id,)
                    ).fetchone()[0]
                return self._db.execute(
                    "SELECT COUNT(*) FROM event_log WHERE issue_id=? AND ts>=?", (issue_id, since)
                ).fetchone()[0]

            c24, c30, c5 = count(), count(now - 1800), count(now - 300)

            row = self._db.execute(
                "SELECT last_sent, step FROM issue_state WHERE issue_id=?", (issue_id,)
            ).fetchone()

            if row is None:                                   # first time we see this issue
                self._db.execute(
                    "INSERT INTO issue_state(issue_id, last_sent, step, title, updated) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (issue_id, now, 1, title, now),
                )
                self._db.commit()
                return True, "new", c24, c30, c5

            last_sent, step = row
            # events since our last message for this issue -> escalation signal
            since_last = self._db.execute(
                "SELECT COUNT(*) FROM event_log WHERE issue_id=? AND ts>?", (issue_id, last_sent)
            ).fetchone()[0]
            escalating = bool(SPIKE_THRESHOLD) and since_last >= SPIKE_THRESHOLD

            gap_needed = self._windows[min(step - 1, len(self._windows) - 1)]
            window_ok = (now - last_sent) >= gap_needed

            if not (window_ok or escalating):
                self._db.commit()                             # persist the event_log insert/prune
                return False, None, c24, c30, c5

            # a send resets the baseline, so escalation needs another THRESHOLD events
            self._db.execute(
                "UPDATE issue_state SET last_sent=?, step=?, title=?, updated=? WHERE issue_id=?",
                (now, step + 1, title, now, issue_id),
            )
            self._db.commit()
            return True, ("escalating" if escalating else "ongoing"), c24, c30, c5

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

        return {
            "issue_id": issue_id,
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
        # line 2: #<last6 of event uuid> · 24h/30m/5m
        short = (p.get("event_id") or "?")[-6:]
        lines.append(
            f"<code>#{esc(short)}</code> · "
            f"{esc(p.get('c24h', 0))}/{esc(p.get('c30m', 0))}/{esc(p.get('c5m', 0))} (24h/30m/5m)"
        )
        lines.append("")

        lines.append(f"<b>{esc(p.get('title'))}</b>")
        if p.get("value") and p.get("value") != p.get("title"):
            lines.append(f"<code>{esc(p['value'])}</code>")
        lines.append("")

        if p.get("culprit"):
            lines.append(f"<b>Culprit:</b> <code>{esc(p['culprit'])}</code>")

        # who last changed the crash line (GitLab blame)
        b = p.get("blame")
        if b and b.get("author"):
            who = esc(b["author"])
            extra = " · ".join(x for x in (
                f"<code>{esc(b['sha'])}</code>" if b.get("sha") else "",
                esc(b["date"]) if b.get("date") else "",
                esc(b["subject"]) if b.get("subject") else "",
            ) if x)
            lines.append(f"<b>Author</b> (L{esc(b.get('line'))}): {who}"
                         + (f" — {extra}" if extra else ""))

        meta = []
        if p.get("level"):      meta.append(f"level {esc(p['level'])}")
        if p.get("count"):      meta.append(f"events {esc(p['count'])}")
        if p.get("user_count"): meta.append(f"users {esc(p['user_count'])}")
        if meta:
            lines.append(" · ".join(meta))

        if p.get("frames"):
            lines.append("<pre>" + "\n".join(esc(f) for f in p["frames"]) + "</pre>")

        if analysis:
            lines += ["", analysis]

        if p.get("url"):
            lines += ["", f'<a href="{esc(p["url"])}">Open in Sentry →</a>']

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
            "date": (commit.get("committed_date") or "")[:10],
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
        # cache hit -> skip the GitLab fetches, prompt build, and LLM call
        if self._llm is not None and issue_id:
            hit = self._analysis_cache.get(issue_id)
            if hit and hit[0] == blame_sha:
                log.info("llm cache hit issue=%s commit=%s", issue_id, blame_sha[:8])
                return hit[1]

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
            text = "".join(
                b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"
            ).strip()
            result = ("🤖 " + esc(text)) if text else None
            if result and issue_id:
                self._analysis_cache[issue_id] = (blame_sha, result)
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
            # record the occurrence, get counts, and decide send + status atomically
            send, status, p["c24h"], p["c30m"], p["c5m"] = \
                await self.register_and_decide(p["issue_id"], p.get("title") or "")
            if not send:
                log.info("debounced issue=%s (24h/30m/5m=%s/%s/%s)",
                         p["issue_id"], p["c24h"], p["c30m"], p["c5m"])
                # debug: build + log the prompt for debounced events too
                if LOG_LLM_PROMPT:
                    p["_loc"] = await self.locate_source(p)
                    if p["_loc"]:
                        p["blame"] = await self.fetch_blame(p["_loc"])
                    await self.analyze(p)     # logs the prompt; returns None while LLM off
                return
            p["status"] = status
            # numeric project id -> name (None falls back to "sentry" in the header)
            p["project"] = await self.resolve_project(p.get("project"))
            # locate the crash file once -> reused for blame (author) and source
            p["_loc"] = await self.locate_source(p)
            if p["_loc"]:
                p["blame"] = await self.fetch_blame(p["_loc"])
            analysis = await self.analyze(p)              # None until ENABLE_LLM=true
            await self._send(self.build_message(p, analysis))
            log.info("sent issue=%s status=%s (24h/30m/5m=%s/%s/%s)",
                     p["issue_id"], status, p["c24h"], p["c30m"], p["c5m"])
        except Exception as e:
            log.exception("process failed: %s", e)
