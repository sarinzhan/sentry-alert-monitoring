"""RulesRepo — per-chat trigger-rule overrides layered over the global defaults."""
from app.config import DEFAULT_RULES, ALERT_STATUSES

RULE_COLUMNS = {"ongoing_sec", "critical_window_sec", "critical_threshold",
                "affected_user_threshold", "critical_ratelimit_sec",
                "project_window_sec", "stat_windows",
                # incident subsystem per-chat overrides (incident_enabled is a 0/1 toggle)
                "incident_enabled", "incident_window_sec",
                "incident_error_threshold", "incident_user_threshold"}

# the columns effective()/overrides() select, in order
_SELECT_COLS = ("statuses", "ongoing_sec", "critical_window_sec", "critical_threshold",
                "affected_user_threshold", "critical_ratelimit_sec", "project_window_sec",
                "stat_windows", "incident_enabled", "incident_window_sec",
                "incident_error_threshold", "incident_user_threshold")
_SELECT_SQL = ", ".join(_SELECT_COLS)


class RulesRepo:
    def __init__(self, conn):
        self.db = conn

    def effective(self, chat_id):
        """A chat's trigger rules: its overrides layered over the global DEFAULT_RULES."""
        r = dict(DEFAULT_RULES)
        r["stat_windows"] = list(DEFAULT_RULES["stat_windows"])
        row = self.db.execute(
            f"SELECT {_SELECT_SQL} FROM chat_rules WHERE chat_id=?", (str(chat_id),)).fetchone()
        if not row:
            return r
        (statuses, ongoing, cwin, cthr, athr, crl, pwin, swins,
         inc_en, inc_win, inc_ethr, inc_uthr) = row
        if statuses is not None:            # None = all; "" = none (mute errors)
            r["statuses"] = set(x for x in statuses.split(",") if x)
        if ongoing is not None: r["ongoing_sec"] = ongoing
        if cwin is not None:    r["critical_window_sec"] = cwin
        if cthr is not None:    r["critical_threshold"] = cthr
        if athr is not None:    r["affected_user_threshold"] = athr
        if crl is not None:     r["critical_ratelimit_sec"] = crl
        if pwin is not None:    r["project_window_sec"] = pwin
        if swins:
            wins = [int(x) for x in swins.split(",") if x.strip().isdigit()]
            if wins:
                r["stat_windows"] = wins
        if inc_en is not None:   r["incident_enabled"] = bool(inc_en)
        if inc_win is not None:  r["incident_window_sec"] = inc_win
        if inc_ethr is not None: r["incident_error_threshold"] = inc_ethr
        if inc_uthr is not None: r["incident_user_threshold"] = inc_uthr
        return r

    def chat_ids(self):
        """Chat ids that have a per-chat rules row (any override, incl. statuses)."""
        return [r[0] for r in self.db.execute(
            "SELECT chat_id FROM chat_rules").fetchall()]

    def overrides(self, chat_id):
        """Which rule columns this chat overrides (non-NULL), as a list. 'statuses'
        is included when the chat restricts statuses. Empty = pure defaults."""
        row = self.db.execute(
            f"SELECT {_SELECT_SQL} FROM chat_rules WHERE chat_id=?", (str(chat_id),)).fetchone()
        if not row:
            return []
        return [c for c, v in zip(_SELECT_COLS, row) if v is not None]

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
        """Set which error statuses a chat receives.
        None -> all (NULL); empty -> none / mute errors (""); subset -> those.
        A chat can mute errors and keep only incidents (incident_enabled)."""
        chat_id = str(chat_id)
        csv = None if statuses is None else ",".join(s for s in ALERT_STATUSES if s in statuses)
        self.db.execute("INSERT OR IGNORE INTO chat_rules(chat_id) VALUES (?)", (chat_id,))
        self.db.execute("UPDATE chat_rules SET statuses=? WHERE chat_id=?", (csv, chat_id))
        self.db.commit()
        return True

    def reset(self, chat_id):
        """Drop per-chat threshold/window overrides (back to global defaults).
        Keeps the opt-ins: statuses, subscriptions, and the incident_enabled toggle."""
        self.db.execute(
            "UPDATE chat_rules SET ongoing_sec=NULL, critical_window_sec=NULL,"
            " critical_threshold=NULL, affected_user_threshold=NULL,"
            " critical_ratelimit_sec=NULL, project_window_sec=NULL,"
            " stat_windows=NULL, incident_window_sec=NULL,"
            " incident_error_threshold=NULL, incident_user_threshold=NULL"
            " WHERE chat_id=?", (str(chat_id),))
        self.db.commit()
