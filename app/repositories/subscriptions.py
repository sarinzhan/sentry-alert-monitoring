"""SubscriptionsRepo — which chats receive which projects (opt-in, per chat)."""
import time

from app.config import PROJECT_NAMES, GITLAB_PROJECTS


class SubscriptionsRepo:
    def __init__(self, conn):
        self.db = conn

    @staticmethod
    def projects_overview():
        """The configured project catalog (SENTRY_PROJECTS / GITLAB_PROJECTS)."""
        ids = set(PROJECT_NAMES) | set(GITLAB_PROJECTS)
        return [{"id": pid, "name": PROJECT_NAMES.get(pid), "repo": GITLAB_PROJECTS.get(pid)}
                for pid in sorted(ids, key=str)]

    def subscribe(self, chat_id, project, thread_id=None, by=None):
        """Subscribe a chat to a project id/slug (or '*' for all). Records where to post."""
        project = str(project).strip().lstrip("#")
        if not project:
            return False
        self.db.execute(
            "INSERT OR REPLACE INTO chat_subscription(chat_id, project, thread_id, by, at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(chat_id), project, str(thread_id) if thread_id is not None else None,
             by, time.time()))
        self.db.commit()
        return True

    def unsubscribe(self, chat_id, project):
        project = str(project).strip().lstrip("#")
        cur = self.db.execute("DELETE FROM chat_subscription WHERE chat_id=? AND project=?",
                              (str(chat_id), project))
        self.db.commit()
        return cur.rowcount

    def list_for(self, chat_id):
        return [r[0] for r in self.db.execute(
            "SELECT project FROM chat_subscription WHERE chat_id=? ORDER BY project", (str(chat_id),)
        ).fetchall()]

    def chats_for(self, *keys):
        """Chats subscribed to any of the given project keys or to '*'.
        Returns [(chat_id, thread_id)], de-duplicated per chat (prefers a topic thread)."""
        cand = list(dict.fromkeys(str(k) for k in keys if k not in (None, ""))) + ["*"]
        q = "SELECT chat_id, thread_id FROM chat_subscription WHERE project IN (%s)" % \
            ",".join("?" * len(cand))
        out = {}
        for cid, tid in self.db.execute(q, cand).fetchall():
            if cid not in out or (tid and not out[cid]):
                out[cid] = tid
        return list(out.items())
