"""WebRequestsRepo — history of web /api/explain runs: the form as submitted,
the final answer, token usage and the llm_call audit id («История» tab).
Each run records the logged-in username, so the admin screen can show a
per-user history log."""
import time

FORM_FIELDS = ("description", "request_id", "device_id", "msisdn",
               "period", "date_from", "date_to", "environment")

COLS = ("id", "at", "username", "own_token", "description", "request_id",
        "device_id", "msisdn", "period", "date_from", "date_to", "environment",
        "llm_id", "response", "in_tokens", "out_tokens", "cost_usd",
        "duration_ms", "error")


class WebRequestsRepo:
    def __init__(self, conn):
        self.db = conn

    def add(self, form, rec=None, llm_id=None, error=None, username=None,
            own_token=False):
        """Persist one run. form is the request body; rec the LlmCall (None
        when the run failed — pass error instead). own_token marks a run on
        the user's personal Claude token. Returns the new row id."""
        cur = self.db.execute(
            "INSERT INTO web_request(at, username, own_token, description,"
            " request_id, device_id, msisdn, period, date_from, date_to,"
            " environment, llm_id, response, in_tokens, out_tokens, cost_usd,"
            " duration_ms, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(),
             username,
             1 if own_token else 0,
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

    def count_shared_since(self, username, since_ts):
        """How many runs the user made on the SHARED token since since_ts —
        drives the daily request quota. Personal-token runs are excluded;
        pre-migration rows (own_token NULL) count as shared."""
        return self.db.execute(
            "SELECT COUNT(*) FROM web_request WHERE username=? AND at>=?"
            " AND (own_token IS NULL OR own_token=0)",
            (username, since_ts)).fetchone()[0]

    def tokens_shared_since(self, username, since_ts):
        """Total tokens (in+out) the user spent on the SHARED token since
        since_ts — drives the daily token quota."""
        return self.db.execute(
            "SELECT COALESCE(SUM(COALESCE(in_tokens,0)+COALESCE(out_tokens,0)),0)"
            " FROM web_request WHERE username=? AND at>=?"
            " AND (own_token IS NULL OR own_token=0)",
            (username, since_ts)).fetchone()[0]

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
