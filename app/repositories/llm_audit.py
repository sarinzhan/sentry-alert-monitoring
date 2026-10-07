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

    @staticmethod
    def _where(kinds, since, until, exclude=None):
        """(where-clause, args) shared by list() and totals(). `kinds` includes
        only those kinds; `exclude` drops them (used for the 'user calls' view =
        everything except the webhook kinds)."""
        clauses, args = [], []
        kinds = list(kinds or [])
        exclude = list(exclude or [])
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            args += kinds
        if exclude:
            clauses.append("kind NOT IN (%s)" % ",".join("?" * len(exclude)))
            args += exclude
        if since is not None:
            clauses.append("at >= ?")
            args.append(float(since))
        if until is not None:
            clauses.append("at <= ?")
            args.append(float(until))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def list(self, kinds=None, limit=100, offset=0, since=None, until=None, exclude=None):
        """Recent calls (newest first), optionally filtered by kind and time range
        (since/until = epoch seconds). Summary rows — response preview + tool-call
        count; fetch get(id) for the full step trace."""
        cols = ("id", "at", "kind", "issue_id", "chat_id", "model", "agentic",
                "turns", "in_tokens", "out_tokens", "cost_usd", "duration_ms",
                "response", "tool_calls")
        where, args = self._where(kinds, since, until, exclude)
        q = f"SELECT {', '.join(cols)} FROM llm_call{where} ORDER BY at DESC LIMIT ? OFFSET ?"
        out = []
        for row in self.db.execute(q, args + [int(limit), int(offset)]).fetchall():
            d = dict(zip(cols, row))
            d["agentic"] = bool(d["agentic"])
            calls = json.loads(d.pop("tool_calls") or "[]")
            d["tool_count"] = len(calls)
            resp = (d.pop("response") or "")
            d["preview"] = resp[:200] + ("…" if len(resp) > 200 else "")
            out.append(d)
        return out

    def totals(self, kinds=None, since=None, until=None, exclude=None):
        """Aggregate over the whole filtered set (ignores paging):
        {count, in_tokens, out_tokens, cost_usd}."""
        where, args = self._where(kinds, since, until, exclude)
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(in_tokens),0), COALESCE(SUM(out_tokens),0),"
            " COALESCE(SUM(cost_usd),0) FROM llm_call" + where, args).fetchone()
        return {"count": row[0], "in_tokens": row[1], "out_tokens": row[2],
                "cost_usd": row[3]}

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
