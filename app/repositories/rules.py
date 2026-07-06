"""RulesRepo — per-chat trigger-rule overrides layered over the global defaults."""
from app.config import DEFAULT_RULES, ALERT_STATUSES

RULE_COLUMNS = {"ongoing_sec", "critical_window_sec", "critical_threshold",
                "affected_user_threshold", "critical_ratelimit_sec", "stat_windows"}


class RulesRepo:
    def __init__(self, conn):
        self.db = conn

    def effective(self, chat_id):
        """A chat's trigger rules: its overrides layered over the global DEFAULT_RULES."""
        r = dict(DEFAULT_RULES)
        r["stat_windows"] = list(DEFAULT_RULES["stat_windows"])
        row = self.db.execute(
            "SELECT statuses, ongoing_sec, critical_window_sec, critical_threshold,"
            " affected_user_threshold, critical_ratelimit_sec, stat_windows"
            " FROM chat_rules WHERE chat_id=?", (str(chat_id),)).fetchone()
        if not row:
            return r
        statuses, ongoing, cwin, cthr, athr, crl, swins = row
        if statuses is not None:
            r["statuses"] = set(x for x in statuses.split(",") if x)
        if ongoing is not None: r["ongoing_sec"] = ongoing
        if cwin is not None:    r["critical_window_sec"] = cwin
        if cthr is not None:    r["critical_threshold"] = cthr
        if athr is not None:    r["affected_user_threshold"] = athr
        if crl is not None:     r["critical_ratelimit_sec"] = crl
        if swins:
            wins = [int(x) for x in swins.split(",") if x.strip().isdigit()]
            if wins:
                r["stat_windows"] = wins
        return r

    def set_rule(self, chat_id, column, value):
        """Set one per-chat rule override (column must be in RULE_COLUMNS)."""
        if column not in RULE_COLUMNS:
            return False
        chat_id = str(chat_id)
        self.db.execute("INSERT OR IGNORE INTO chat_rules(chat_id) VALUES (?)", (chat_id,))
        self.db.execute(f"UPDATE chat_rules SET {column}=? WHERE chat_id=?", (value, chat_id))
        self.db.commit()
        return True

    def set_statuses(self, chat_id, statuses):
        """Set which alert statuses a chat receives. statuses=None -> all."""
        chat_id = str(chat_id)
        csv = None if not statuses else ",".join(s for s in ALERT_STATUSES if s in statuses)
        self.db.execute("INSERT OR IGNORE INTO chat_rules(chat_id) VALUES (?)", (chat_id,))
        self.db.execute("UPDATE chat_rules SET statuses=? WHERE chat_id=?", (csv, chat_id))
        self.db.commit()
        return True

    def reset(self, chat_id):
        """Drop all per-chat rule overrides (back to global defaults). Keeps statuses/subs."""
        self.db.execute(
            "UPDATE chat_rules SET ongoing_sec=NULL, critical_window_sec=NULL,"
            " critical_threshold=NULL, affected_user_threshold=NULL,"
            " critical_ratelimit_sec=NULL, stat_windows=NULL WHERE chat_id=?", (str(chat_id),))
        self.db.commit()
