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
import hmac
import asyncio
import hashlib
import sqlite3

from config import (
    CLIENT_SECRET, DB_PATH, SEND_WINDOWS,
    ENABLE_LLM, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, log,
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
        self._db.commit()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- security
    @staticmethod
    def verify(body: bytes, sig) -> bool:
        if not CLIENT_SECRET:
            return True
        if not sig:
            return False
        expected = hmac.new(CLIENT_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

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
        lines = [f"{emoji} <b>Sentry · {esc(p.get('project') or 'sentry')}</b>", ""]

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

    # ------------------------------------------------------------- pipeline
    async def process(self, resource: str, payload: dict):
        try:
            p = self.parse(resource, payload)
            if not p:
                log.info("ignored: nothing to parse")
                return
            if p.get("action") not in NOTIFY_ACTIONS:
                log.info("ignored action=%s issue=%s", p.get("action"), p["issue_id"])
                return
            if not await self.should_send(p["issue_id"], p.get("title") or ""):
                log.info("debounced issue=%s", p["issue_id"])
                return
            analysis = await self.analyze(p)              # None today
            await self._send(self.build_message(p, analysis))
            log.info("sent issue=%s", p["issue_id"])
        except Exception as e:
            log.exception("process failed: %s", e)
