"""WebRequestsRepo — history of web /api/explain runs: the form as submitted,
the final answer, token usage and the llm_call audit id («История» tab)."""
import time

FORM_FIELDS = ("description", "request_id", "device_id", "msisdn",
               "period", "date_from", "date_to", "environment")


class WebRequestsRepo:
    def __init__(self, conn):
        self.db = conn

    def add(self, form, rec=None, llm_id=None, error=None):
        """Persist one run. form is the request body; rec the LlmCall (None
        when the run failed — pass error instead). Returns the new row id."""
        cur = self.db.execute(
            "INSERT INTO web_request(at, description, request_id, device_id,"
            " msisdn, period, date_from, date_to, environment, llm_id,"
            " response, in_tokens, out_tokens, cost_usd, duration_ms, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(),
             *((form.get(f) or "").strip() or None for f in FORM_FIELDS),
             llm_id,
             rec.text if rec else None,
             rec.in_tokens if rec else None,
             rec.out_tokens if rec else None,
             rec.cost if rec else None,
             rec.duration_ms if rec else None,
             error))
        self.db.commit()
        return cur.lastrowid

    def list(self, limit=100):
        cols = ("id", "at", "description", "request_id", "device_id", "msisdn",
                "period", "date_from", "date_to", "environment", "llm_id",
                "response", "in_tokens", "out_tokens", "cost_usd",
                "duration_ms", "error")
        rows = self.db.execute(
            f"SELECT {', '.join(cols)} FROM web_request ORDER BY at DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(zip(cols, r)) for r in rows]
