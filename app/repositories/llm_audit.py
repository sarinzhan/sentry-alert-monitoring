"""LlmAuditRepo — every LLM call persisted in full: prompt, tools, trace, result.

Each saved call gets a short id (`llm_a1b2c3`) that the bot appends to its
reply; /llm <id> and GET /api/llm/{id} look it up (case-insensitive, the
`llm_` prefix optional).
"""
import json
import time
import secrets

_KEYS = ("id", "at", "kind", "issue_id", "chat_id", "model", "auth", "agentic",
         "prompt", "tools_offered", "tool_calls", "turns", "in_tokens",
         "out_tokens", "cost_usd", "duration_ms", "response")


class LlmAuditRepo:
    def __init__(self, conn):
        self.db = conn

    def put(self, kind, rec, issue_id=None, chat_id=None):
        """Persist one LlmCall record (services.llm). Returns the new audit id."""
        while True:
            llm_id = "llm_" + secrets.token_hex(3)
            if not self.db.execute("SELECT 1 FROM llm_call WHERE id=?", (llm_id,)).fetchone():
                break
        self.db.execute(
            f"INSERT INTO llm_call({', '.join(_KEYS)}) VALUES ({', '.join('?' * len(_KEYS))})",
            (llm_id, time.time(), kind, issue_id,
             str(chat_id) if chat_id is not None else None,
             rec.model, rec.auth, int(rec.agentic), rec.prompt,
             json.dumps(rec.tools_offered), json.dumps(rec.tool_calls, default=str),
             rec.turns, rec.in_tokens, rec.out_tokens, rec.cost, rec.duration_ms,
             rec.text))
        self.db.commit()
        return llm_id

    def get(self, ref):
        """Full record dict by id — accepts 'llm_a1b2c3' or 'a1b2c3', any case."""
        ref = (ref or "").strip().lower()
        if not ref.startswith("llm_"):
            ref = "llm_" + ref
        row = self.db.execute(
            f"SELECT {', '.join(_KEYS)} FROM llm_call WHERE id=?", (ref,)).fetchone()
        if not row:
            return None
        rec = dict(zip(_KEYS, row))
        rec["agentic"] = bool(rec["agentic"])
        rec["tools_offered"] = json.loads(rec["tools_offered"] or "[]")
        rec["tool_calls"] = json.loads(rec["tool_calls"] or "[]")
        return rec
