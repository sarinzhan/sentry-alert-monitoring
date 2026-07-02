"""
SentryEventHandler — everything specific to Sentry webhooks.

Responsibilities:
  - verify the webhook signature
  - normalize the different Sentry payload shapes
  - debounce per issue (SQLite-backed, survives restarts)
  - format the Telegram message (+ optional LLM cause/fix)
  - send via an injected sender (ChatBotHandler.send)
"""
import time
import json
import hmac
import asyncio
import hashlib
import sqlite3

import httpx

from config import (
    CLIENT_SECRET, DB_PATH, SEND_WINDOWS,
    SPIKE_THRESHOLD, SPIKE_WINDOW, SPIKE_COOLDOWN,
    ENABLE_LLM, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, LOG_RAW_PAYLOAD,
    SENTRY_API_URL, SENTRY_ORG, SENTRY_API_TOKEN, log,
)
from utils import esc


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
        self._last_spike: dict[str, float] = {}   # issue_id -> last spike-alert time

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

    # ------------------------------------------------------------- counting
    def record_and_count(self, issue_id: str):
        """Record one occurrence; return (total_24h, count_in_window) for this issue."""
        now = time.time()
        self._db.execute("INSERT INTO event_log (issue_id, ts) VALUES (?, ?)", (issue_id, now))
        self._db.execute("DELETE FROM event_log WHERE ts < ?", (now - 86400,))  # prune >24h
        self._db.commit()
        total = self._db.execute(
            "SELECT COUNT(*) FROM event_log WHERE issue_id = ?", (issue_id,)
        ).fetchone()[0]
        window = self._db.execute(
            "SELECT COUNT(*) FROM event_log WHERE issue_id = ? AND ts >= ?",
            (issue_id, now - SPIKE_WINDOW),
        ).fetchone()[0]
        return total, window

    def spike_triggered(self, issue_id: str, window_count: int) -> bool:
        """True if this issue crossed the rate threshold and isn't in spike cooldown."""
        if not SPIKE_THRESHOLD or window_count < SPIKE_THRESHOLD:
            return False
        now = time.time()
        if now - self._last_spike.get(issue_id, 0.0) < SPIKE_COOLDOWN:
            return False
        self._last_spike[issue_id] = now
        return True

    async def aclose(self):
        if self._api is not None:
            await self._api.aclose()

    # ---------------------------------------------------------- project names
    async def resolve_project(self, project):
        """Map a numeric project id to its name/slug. Returns a name, or None."""
        if project is None:
            return None
        s = str(project)
        if not s.isdigit():
            return s                       # already a slug/name (issue payloads)
        if self._api is None:
            return None                    # lookup disabled -> caller falls back
        if s in self._proj_cache:
            return self._proj_cache[s]
        await self._refresh_projects()
        return self._proj_cache.get(s)

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

    # ------------------------------------------------------------- debounce
    async def should_send(self, issue_id: str, title: str) -> bool:
        """
        Decide whether to send for this issue right now, and record the decision.
        Runs under a lock so two webhooks for the same issue can't both pass.
        """
        now = time.time()
        async with self._lock:
            row = self._db.execute(
                "SELECT last_sent, step FROM issue_state WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()

            if row is None:                               # first time we see this issue
                self._db.execute(
                    "INSERT INTO issue_state(issue_id, last_sent, step, title, updated) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (issue_id, now, 1, title, now),
                )
                self._db.commit()
                return True

            last_sent, step = row
            gap_needed = self._windows[min(step - 1, len(self._windows) - 1)]
            if now - last_sent < gap_needed:              # still inside window -> skip
                return False

            self._db.execute(
                "UPDATE issue_state SET last_sent=?, step=?, title=?, updated=? "
                "WHERE issue_id=?",
                (now, step + 1, title, now, issue_id),
            )
            self._db.commit()
            return True

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
        frames = []
        values = (obj.get("exception") or {}).get("values") or []
        if values:
            last = values[-1]
            exc_type = exc_type or last.get("type")
            exc_value = exc_value or last.get("value")
            st = (last.get("stacktrace") or {}).get("frames") or []
            for f in reversed(st[-5:]):                   # crash site first, top 5
                fn = f.get("function") or "?"
                where = f.get("filename") or f.get("module") or "?"
                lineno = f.get("lineno")
                frames.append(f"{where}:{lineno} in {fn}" if lineno else f"{where} in {fn}")

        if exc_value:
            exc_value = str(exc_value)[:1000]

        project = obj.get("project")
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
            "project": project,
        }

    # ------------------------------------------------------------- format
    @staticmethod
    def build_message(p: dict, analysis: str = None) -> str:
        emoji = LEVEL_EMOJI.get((p.get("level") or "").lower(), "🔴")
        project = esc(p.get("project") or "sentry")
        if p.get("spike"):
            mins = max(1, SPIKE_WINDOW // 60)
            lines = [f"🚨 <b>SPIKE · {project}</b> — {esc(p.get('last5m'))} errors in {mins} min", ""]
        else:
            lines = [f"{emoji} <b>Sentry · {project}</b>", ""]

        lines.append(f"<b>{esc(p.get('title'))}</b>")
        if p.get("value") and p.get("value") != p.get("title"):
            lines.append(f"<code>{esc(p['value'])}</code>")
        lines.append("")

        if p.get("culprit"):
            lines.append(f"<b>Culprit:</b> <code>{esc(p['culprit'])}</code>")

        meta = []
        if p.get("level"):       meta.append(f"level {esc(p['level'])}")
        if p.get("environment"): meta.append(f"env {esc(p['environment'])}")
        if p.get("count"):       meta.append(f"events {esc(p['count'])}")
        if p.get("user_count"):  meta.append(f"users {esc(p['user_count'])}")
        if meta:
            lines.append(" · ".join(meta))

        # occurrences observed by this notifier (error webhooks carry no aggregate)
        if p.get("total") is not None:
            lines.append(f"<b>Occurrences:</b> {esc(p['total'])} total · {esc(p['last5m'])} in last 5 min")

        if p.get("event_id"):
            lines.append(f"<b>Event:</b> <code>{esc(p['event_id'])}</code>")

        if p.get("frames"):
            lines.append("<pre>" + "\n".join(esc(f) for f in p["frames"]) + "</pre>")

        if analysis:
            lines += ["", analysis]

        if p.get("url"):
            lines += ["", f'<a href="{esc(p["url"])}">Open in Sentry →</a>']

        return "\n".join(lines)

    # ------------------------------------------------------------- LLM (future)
    async def analyze(self, p: dict):
        """
        Ask an LLM for a likely cause + suggested fix.
        Returns None unless ENABLE_LLM=true and an API key is set, so today this
        is a no-op. Uses httpx directly -> no extra dependency to install now.
        """
        if not (ENABLE_LLM and ANTHROPIC_API_KEY):
            return None

        prompt = (
            "You are a senior backend engineer triaging a Sentry error. "
            "Reply in at most 4 short lines, plain text:\n"
            "Likely cause: <one sentence>\n"
            "Suggested fix: <one or two sentences>\n\n"
            f"Type: {p.get('type')}\n"
            f"Message: {p.get('value')}\n"
            f"Culprit: {p.get('culprit')}\n"
            "Stack:\n" + "\n".join(p.get("frames") or [])
        )
        try:
            r = await self._client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            r.raise_for_status()
            text = "".join(
                b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"
            ).strip()
            return ("🤖 " + esc(text)) if text else None
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
            # count every occurrence (incl. debounced ones) before the send decision
            p["total"], p["last5m"] = self.record_and_count(p["issue_id"])
            normal = await self.should_send(p["issue_id"], p.get("title") or "")
            spike = self.spike_triggered(p["issue_id"], p["last5m"])
            if not (normal or spike):
                log.info("debounced issue=%s (total=%s window=%s)",
                         p["issue_id"], p["total"], p["last5m"])
                return
            # show the spike banner whenever the issue is over threshold at send time,
            # no matter which trigger (window or threshold) produced this send
            p["spike"] = bool(SPIKE_THRESHOLD) and p["last5m"] >= SPIKE_THRESHOLD
            # numeric project id -> name (None falls back to "sentry" in the header)
            p["project"] = await self.resolve_project(p.get("project"))
            analysis = await self.analyze(p)              # None today
            await self._send(self.build_message(p, analysis))
            log.info("sent issue=%s%s (total=%s window=%s)",
                     p["issue_id"], " SPIKE" if p.get("spike") else "", p["total"], p["last5m"])
        except Exception as e:
            log.exception("process failed: %s", e)
