"""ProjectsRepo — the project catalog: sentry project id -> name + gitlab repo.

Historically this mapping lived in env (SENTRY_PROJECTS / GITLAB_PROJECTS) and
every module reads the config.PROJECT_NAMES / config.GITLAB_PROJECTS dicts at
call time. The DB table is now the source of truth: env seeds it on first run,
and _sync() rewrites those SAME dicts in place after every change — so all
existing call sites (pipeline, /why, gitlab tools, web explain) pick up web
edits immediately, no restart needed.

Unknown projects (first seen in a webhook or the Sentry API) are auto-
registered with empty fields; the web UI (/admin-web/, «Проекты») fills in
the gitlab link.
"""
import time

from app import config


class ProjectsRepo:
    def __init__(self, conn):
        self.db = conn
        self._seed_from_env()
        self._sync()

    def _seed_from_env(self):
        """Insert env-configured projects that the table doesn't know yet
        (the DB wins for ids it already has)."""
        now = time.time()
        for pid in set(config.PROJECT_NAMES) | set(config.GITLAB_PROJECTS):
            self.db.execute(
                "INSERT OR IGNORE INTO project(id, name, gitlab_repo, first_seen, updated)"
                " VALUES (?, ?, ?, ?, ?)",
                (pid, config.PROJECT_NAMES.get(pid),
                 config.GITLAB_PROJECTS.get(pid), now, now))
        self.db.commit()

    def _sync(self):
        """Rebuild the config dicts in place from the table (empty values are
        left out, matching the old env-parsing behavior)."""
        names, repos = {}, {}
        for pid, name, repo in self.db.execute(
                "SELECT id, name, gitlab_repo FROM project"):
            if name:
                names[pid] = name
            if repo:
                repos[pid] = repo
        config.PROJECT_NAMES.clear()
        config.PROJECT_NAMES.update(names)
        config.GITLAB_PROJECTS.clear()
        config.GITLAB_PROJECTS.update(repos)

    def all(self):
        return [{"id": pid, "slug": slug, "name": name, "gitlab_repo": repo,
                 "first_seen": first, "updated": updated, "audit": audit,
                 "audit_at": audit_at, "audit_llm_id": audit_llm_id}
                for pid, slug, name, repo, first, updated, audit, audit_at,
                    audit_llm_id in self.db.execute(
                    "SELECT id, slug, name, gitlab_repo, first_seen, updated, "
                    "audit, audit_at, audit_llm_id "
                    "FROM project ORDER BY CAST(id AS INTEGER), id")]

    def get(self, pid):
        row = next((p for p in self.all() if p["id"] == str(pid).strip()), None)
        return row

    def set_audit(self, pid, text, llm_id=None):
        """Store the LLM observability verdict shown in the «Проекты» tab."""
        self.db.execute(
            "UPDATE project SET audit=?, audit_at=?, audit_llm_id=? WHERE id=?",
            (text, time.time(), llm_id, str(pid).strip()))
        self.db.commit()

    def ensure(self, pid, slug=None):
        """Auto-register a project seen in a webhook/API response. The sentry
        slug is authoritative — kept up to date whenever we learn it — and
        also fills an empty display name; user edits are never overwritten."""
        pid = str(pid).strip()
        if not pid:
            return
        now = time.time()
        row = self.db.execute("SELECT slug, name FROM project WHERE id=?",
                              (pid,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO project(id, slug, name, gitlab_repo, first_seen, updated)"
                " VALUES (?, ?, ?, NULL, ?, ?)", (pid, slug, slug, now, now))
        elif slug and slug != row[0]:
            self.db.execute("UPDATE project SET slug=?, name=COALESCE(name, ?), "
                            "updated=? WHERE id=?", (slug, slug, now, pid))
        elif slug and not row[1]:
            self.db.execute("UPDATE project SET name=?, updated=? WHERE id=?",
                            (slug, now, pid))
        else:
            return
        self.db.commit()
        self._sync()

    def set(self, pid, name=None, gitlab_repo=None):
        """Web edit: update name and/or gitlab repo ('' clears the value).
        Returns the updated row, or None for an unknown id."""
        pid = str(pid).strip()
        if not self.db.execute("SELECT 1 FROM project WHERE id=?", (pid,)).fetchone():
            return None
        sets, args = [], []
        if name is not None:
            sets.append("name=?")
            args.append(name.strip() or None)
        if gitlab_repo is not None:
            sets.append("gitlab_repo=?")
            args.append(gitlab_repo.strip() or None)
        if sets:
            self.db.execute(f"UPDATE project SET {', '.join(sets)}, updated=? WHERE id=?",
                            (*args, time.time(), pid))
            self.db.commit()
            self._sync()
        return self.get(pid)
