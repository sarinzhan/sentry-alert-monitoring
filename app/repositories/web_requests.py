"""WebRequestsRepo — history of web /api/explain runs: the form as submitted,
the final answer, token usage and the llm_call audit id («История» tab).
Each run records the logged-in username, so the admin screen can show a
per-user history log."""
import time

FORM_FIELDS = ("description", "request_id", "device_id", "msisdn",
               "period", "date_from", "date_to", "environment")

COLS = ("id", "at", "username", "description", "request_id", "device_id",
        "msisdn", "period", "date_from", "date_to", "environment", "llm_id",
        "response", "in_tokens", "out_tokens", "cost_usd",
        "duration_ms", "error")


class WebRequestsRepo:
    def __init__(self, conn):
        self.db = conn

    def add(self, form, rec=None, llm_id=None, error=None, username=None):
        """Persist one run. form is the request body; rec the LlmCall (None
        when the run failed — pass error instead). Returns the new row id."""
        cur = self.db.execute(
            "INSERT INTO web_request(at, username, description, request_id,"
            " device_id, msisdn, period, date_from, date_to, environment,"
            " llm_id, response, in_tokens, out_tokens, cost_usd, duration_ms,"
            " error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(),
             username,
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
        rows = self.db.execute(
            f"SELECT {', '.join(COLS)} FROM web_request ORDER BY at DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(zip(COLS, r)) for r in rows]

    def list_for(self, username, limit=100):
        """One user's runs — the history log on the admin's users screen."""
        rows = self.db.execute(
            f"SELECT {', '.join(COLS)} FROM web_request WHERE username=?"
            " ORDER BY at DESC LIMIT ?", (username, int(limit))).fetchall()
        return [dict(zip(COLS, r)) for r in rows]
