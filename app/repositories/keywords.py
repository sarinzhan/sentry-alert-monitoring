"""KeywordsRepo — keyword force-send list (global or per-project)."""
import time


class KeywordsRepo:
    def __init__(self, conn):
        self.db = conn

    def match(self, text, p):
        """Return the first watched keyword contained in text (global or this project)."""
        t = (text or "").lower()
        if not t:
            return None
        projkeys = [k for k in (str(p.get("project_id")), p.get("project")) if k]
        for kw, proj in self.db.execute("SELECT text, project FROM keyword").fetchall():
            if kw and kw in t and (proj is None or proj in projkeys):
                return kw
        return None

    def add(self, text, project, by):
        text = (text or "").strip().lower()
        if not text:
            return False
        project = str(project).strip() if project else None
        if self.db.execute("SELECT 1 FROM keyword WHERE text=? AND (project IS ? OR project=?)",
                           (text, project, project)).fetchone():
            return False
        self.db.execute("INSERT INTO keyword(text, project, by, at) VALUES (?, ?, ?, ?)",
                        (text, project, by, time.time()))
        self.db.commit()
        return True

    def delete(self, text, project):
        text = (text or "").strip().lower()
        project = str(project).strip() if project else None
        cur = self.db.execute("DELETE FROM keyword WHERE text=? AND (project IS ? OR project=?)",
                              (text, project, project))
        self.db.commit()
        return cur.rowcount

    def list_all(self):
        return self.db.execute(
            "SELECT text, project FROM keyword ORDER BY project IS NULL DESC, project, text"
        ).fetchall()
