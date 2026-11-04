"""Decider — the per-chat trigger state machine.

For one (chat, issue), decide whether to alert and with what status, using the
chat's own rules and its own send state. Pure logic over two repos; the pipeline
runs decide() under its lock so the read-modify-write can't race.
"""
from app.config import KEYWORD_MIN_INTERVAL_SEC
from app.summaries import fmt_duration


class Decider:
    def __init__(self, chat_state, issues):
        self._state = chat_state       # ChatStateRepo
        self._issues = issues          # IssuesRepo (for crit_stats)

    def decide(self, chat_id, issue_id, rules, now, forced=False, project=None):
        """
        Returns (send, status, reason).

        status: new | ongoing (>= chat's min gap) | escalating (chat's critical spike).
        reason: short human string (RU) explaining why it fired — the triggering
        threshold with its actual value (e.g. "20 ошиб. за 10м > 15"), or None when
        it did not fire.
        forced: keyword force-send (bypasses the min gap + status filter).
        project: key for the per-project window — at most one message per (chat,
        project) within rules["project_window_sec"], whatever the status; only
        forced (keyword) sends bypass it. A suppression leaves the issue state
        untouched, so the alert is deferred (retries on the next event once the
        window frees), not lost.
        """
        chat_id = str(chat_id)
        statuses = rules.get("statuses")            # None = all
        project_window = rules.get("project_window_sec") or 0

        def allowed(s):
            return forced or statuses is None or s in statuses

        def project_open():
            if forced or not project_window or project is None:
                return True
            return (now - self._state.project_last_sent(chat_id, project)) >= project_window

        row = self._state.get(chat_id, issue_id)
        first_time = row is None
        last_sent, last_critical, step = row or (0.0, 0.0, 0)

        def remember(ls, lc, st):
            self._state.remember(chat_id, issue_id, ls, lc, st)
            if project is not None:
                self._state.remember_project(chat_id, project, ls)

        if first_time:
            if allowed("new"):
                if not project_open():
                    return False, None, None
                remember(now, 0, 1)
                return True, "new", "первое появление ошибки"
            self._state.remember(chat_id, issue_id, 0, 0, 0)  # filtered: record so it can go "ongoing" later
            return False, None, None

        cnt, usrs = self._issues.crit_stats(issue_id, rules["critical_window_sec"], now)
        win = fmt_duration(rules["critical_window_sec"])
        hit_errors = cnt > rules["critical_threshold"]
        hit_users = usrs >= rules["affected_user_threshold"]
        is_critical = hit_errors or hit_users
        critical_allowed = is_critical and (now - (last_critical or 0)) >= rules["critical_ratelimit_sec"]
        ongoing_ok = (now - last_sent) >= rules["ongoing_sec"]

        if critical_allowed and allowed("escalating"):
            status = "escalating"
            parts = []
            if hit_errors:
                parts.append(f"{cnt} ошиб. за {win} > {rules['critical_threshold']}")
            if hit_users:
                parts.append(f"{usrs} польз. за {win} ≥ {rules['affected_user_threshold']}")
            reason = " и ".join(parts)
        elif ongoing_ok and allowed("ongoing"):
            status = "ongoing"
            reason = f"прошло ≥ {fmt_duration(rules['ongoing_sec'])} с последнего алерта"
        elif forced:                                # keyword force-send (bypasses gap + filter)
            if KEYWORD_MIN_INTERVAL_SEC and (now - last_sent) < KEYWORD_MIN_INTERVAL_SEC:
                return False, None, None
            status = "ongoing"
            reason = "совпадение по ключевому слову"
        else:
            return False, None, None

        if forced and status != "escalating":
            reason = "совпадение по ключевому слову"

        if not project_open():
            return False, None, None
        remember(now, now if status == "escalating" else (last_critical or 0), step + 1)
        return True, status, reason
