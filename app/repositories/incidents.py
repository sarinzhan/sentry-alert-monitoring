"""IncidentRepo — org-scoped incidents: the group of issues sharing one root cause.

An incident is opened by the Grouper when a new issue can't be folded into an
existing open incident, grows as more issues join, notifies each subscribed chat
once it crosses that chat's thresholds (one live message, edited in place), and is
closed (status='resolved') by the Resolved button or the idle sweeper.

Pure SQL over the three incident tables (see app/db.py). Read-modify-write that
must stay atomic is serialized by the caller (IncidentService / Grouper locks).
"""
import time


def _merge_projects(existing: str, project: str):
    """Add `project` to the comma-set `existing`; returns (csv, is_new)."""
    cur = [x for x in (existing or "").split(",") if x]
    if not project or project in cur:
        return ",".join(cur), False
    cur.append(project)
    return ",".join(cur), True


class IncidentRepo:
    def __init__(self, conn):
        self.db = conn

    # ------------------------------------------------------------ lookups
    def issue_incident(self, issue_id):
        """The incident this issue already belongs to, or None (any status)."""
        row = self.db.execute(
            "SELECT incident_id FROM incident_member WHERE issue_id=?", (issue_id,)).fetchone()
        return row[0] if row else None

    def open_incidents(self, now=None, window_sec=None):
        """Open incidents, newest activity first. If window_sec is given, only those
        active within it. Each: {incident_id, title, description, projects,
        lead_issue_id, member_count, last_member_at}."""
        q = ("SELECT incident_id, title, description, projects, lead_issue_id,"
             " member_count, last_member_at FROM incident WHERE status='open'")
        args = []
        if window_sec:
            q += " AND last_member_at >= ?"
            args.append((now or time.time()) - window_sec)
        q += " ORDER BY last_member_at DESC"
        return [{"incident_id": r[0], "title": r[1], "description": r[2],
                 "projects": r[3], "lead_issue_id": r[4], "member_count": r[5],
                 "last_member_at": r[6]} for r in self.db.execute(q, args).fetchall()]

    def signature_match(self, signature, now=None, window_sec=None):
        """incident_id of an OPEN incident that already has a member with this exact
        signature (the deterministic fast-path), or None."""
        if not signature:
            return None
        q = ("SELECT m.incident_id FROM incident_member m JOIN incident i"
             " ON i.incident_id=m.incident_id WHERE i.status='open' AND m.signature=?")
        args = [signature]
        if window_sec:
            q += " AND i.last_member_at >= ?"
            args.append((now or time.time()) - window_sec)
        q += " ORDER BY i.last_member_at DESC LIMIT 1"
        row = self.db.execute(q, args).fetchone()
        return row[0] if row else None

    def get(self, incident_id):
        row = self.db.execute(
            "SELECT incident_id, status, title, description, lead_issue_id, projects,"
            " member_count, opened_at, last_member_at, last_escalated_at,"
            " resolved_at, resolved_by, resolution, resolved_kind"
            " FROM incident WHERE incident_id=?", (incident_id,)).fetchone()
        if not row:
            return None
        keys = ("incident_id", "status", "title", "description", "lead_issue_id",
                "projects", "member_count", "opened_at", "last_member_at",
                "last_escalated_at", "resolved_at", "resolved_by", "resolution",
                "resolved_kind")
        return dict(zip(keys, row))

    def list(self, limit=200, status=None):
        """Incidents for the web panel — open first, then most-recent activity.
        status filters to 'open' or 'resolved'; None returns both."""
        q = ("SELECT incident_id, status, title, projects, member_count, opened_at,"
             " last_member_at, resolved_at, resolved_by, resolved_kind, resolution"
             " FROM incident")
        args = []
        if status in ("open", "resolved"):
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY (status='open') DESC, last_member_at DESC LIMIT ?"
        args.append(int(limit))
        keys = ("incident_id", "status", "title", "projects", "member_count",
                "opened_at", "last_member_at", "resolved_at", "resolved_by",
                "resolved_kind", "resolution")
        return [dict(zip(keys, r)) for r in self.db.execute(q, args).fetchall()]

    def members(self, incident_id):
        """All grouped issues of an incident: [{issue_id, signature, project, title,
        short, joined_at}], in join order."""
        rows = self.db.execute(
            "SELECT issue_id, signature, project, title, short, joined_at"
            " FROM incident_member WHERE incident_id=? ORDER BY joined_at", (incident_id,)).fetchall()
        return [{"issue_id": r[0], "signature": r[1], "project": r[2], "title": r[3],
                 "short": r[4], "joined_at": r[5]} for r in rows]

    def member_issue_ids(self, incident_id):
        return [r[0] for r in self.db.execute(
            "SELECT issue_id FROM incident_member WHERE incident_id=?", (incident_id,)).fetchall()]

    # ------------------------------------------------------------ mutations
    def create(self, lead_issue_id, signature, project, title, short, now=None):
        """Open a new incident with `lead_issue_id` as its first member. Returns the id."""
        now = now or time.time()
        cur = self.db.execute(
            "INSERT INTO incident(status, title, lead_issue_id, projects, member_count,"
            " opened_at, last_member_at) VALUES ('open', ?, ?, ?, 1, ?, ?)",
            (title, lead_issue_id, project or "", now, now))
        incident_id = cur.lastrowid
        self.db.execute(
            "INSERT OR IGNORE INTO incident_member(incident_id, issue_id, signature,"
            " project, title, short, joined_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (incident_id, lead_issue_id, signature, project, title, short, now))
        self.db.commit()
        return incident_id

    def add_member(self, incident_id, issue_id, signature, project, title, short, now=None):
        """Fold an issue into an incident. Returns True if it brought a NEW project."""
        now = now or time.time()
        self.db.execute(
            "INSERT OR IGNORE INTO incident_member(incident_id, issue_id, signature,"
            " project, title, short, joined_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (incident_id, issue_id, signature, project, title, short, now))
        row = self.db.execute(
            "SELECT projects FROM incident WHERE incident_id=?", (incident_id,)).fetchone()
        projects, is_new = _merge_projects(row[0] if row else "", project)
        cnt = self.db.execute(
            "SELECT COUNT(*) FROM incident_member WHERE incident_id=?", (incident_id,)).fetchone()[0]
        self.db.execute(
            "UPDATE incident SET member_count=?, last_member_at=?, projects=? WHERE incident_id=?",
            (cnt, now, projects, incident_id))
        self.db.commit()
        return is_new

    def touch(self, incident_id, now=None):
        """Bump activity (a repeat event of an already-grouped issue keeps it open)."""
        self.db.execute("UPDATE incident SET last_member_at=? WHERE incident_id=? AND status='open'",
                        (now or time.time(), incident_id))
        self.db.commit()

    def set_description(self, incident_id, description, title=None):
        if title is not None:
            self.db.execute("UPDATE incident SET description=?, title=? WHERE incident_id=?",
                            (description, title, incident_id))
        else:
            self.db.execute("UPDATE incident SET description=? WHERE incident_id=?",
                            (description, incident_id))
        self.db.commit()

    def mark_escalated(self, incident_id, now=None):
        self.db.execute("UPDATE incident SET last_escalated_at=? WHERE incident_id=?",
                        (now or time.time(), incident_id))
        self.db.commit()

    def resolve(self, incident_id, by=None, resolution=None, kind="manual", now=None):
        self.db.execute(
            "UPDATE incident SET status='resolved', resolved_at=?, resolved_by=?,"
            " resolution=?, resolved_kind=? WHERE incident_id=?",
            (now or time.time(), by, resolution, kind, incident_id))
        self.db.commit()

    def sweep_candidates(self, now, window_sec):
        """Open incidents idle for a full window — due for auto-resolve."""
        return [r[0] for r in self.db.execute(
            "SELECT incident_id FROM incident WHERE status='open' AND last_member_at <= ?",
            (now - window_sec,)).fetchall()]

    # ------------------------------------------------------------ per-chat live message
    def message_for(self, incident_id, chat_id):
        row = self.db.execute(
            "SELECT message_id, thread_id, member_count_at_send FROM incident_message"
            " WHERE incident_id=? AND chat_id=?", (incident_id, str(chat_id))).fetchone()
        if not row:
            return None
        return {"message_id": row[0], "thread_id": row[1], "member_count_at_send": row[2]}

    def all_messages(self, incident_id):
        """Every live message for an incident, for edit-all / delete-all on resolve."""
        return [{"chat_id": r[0], "message_id": r[1], "thread_id": r[2]}
                for r in self.db.execute(
                    "SELECT chat_id, message_id, thread_id FROM incident_message"
                    " WHERE incident_id=?", (incident_id,)).fetchall()]

    def record_message(self, incident_id, chat_id, message_id, thread_id, member_count, now=None):
        self.db.execute(
            "INSERT OR REPLACE INTO incident_message(incident_id, chat_id, message_id,"
            " thread_id, member_count_at_send, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
            (incident_id, str(chat_id), message_id, str(thread_id) if thread_id is not None else None,
             member_count, now or time.time()))
        self.db.commit()

    def update_message_count(self, incident_id, chat_id, member_count):
        self.db.execute(
            "UPDATE incident_message SET member_count_at_send=? WHERE incident_id=? AND chat_id=?",
            (member_count, incident_id, str(chat_id)))
        self.db.commit()

    def delete_messages(self, incident_id):
        self.db.execute("DELETE FROM incident_message WHERE incident_id=?", (incident_id,))
        self.db.commit()
