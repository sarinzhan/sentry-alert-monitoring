"""KnowledgeRepo — the LLM's persistent notes: navigation hints between runs.

One row per topic (case-insensitive: saving an existing topic updates it, so
notes converge instead of piling up). Semantic search lives in
services.knowledge_tools — here only the queries, including the keyword
fallback ranking used while the embedding model is unavailable.
"""
import time

_KEYS = ("id", "topic", "content", "source", "emb_model", "created", "updated")


class KnowledgeRepo:
    def __init__(self, conn):
        self.db = conn

    def save(self, topic, content, source=None, embedding=None, emb_model=None):
        """Insert, or update the existing note with this topic (NOCASE).
        Returns (id, created) — created False when an existing note was updated."""
        topic = " ".join((topic or "").split())
        now = time.time()
        row = self.db.execute("SELECT id FROM knowledge WHERE topic=?",
                              (topic,)).fetchone()
        if row:
            self.db.execute(
                "UPDATE knowledge SET topic=?, content=?, source=?, embedding=?, "
                "emb_model=?, updated=? WHERE id=?",
                (topic, content, source, embedding, emb_model, now, row[0]))
            self.db.commit()
            return row[0], False
        cur = self.db.execute(
            "INSERT INTO knowledge(topic, content, source, embedding, emb_model, "
            "created, updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (topic, content, source, embedding, emb_model, now, now))
        self.db.commit()
        return cur.lastrowid, True

    def get(self, ref):
        """One note dict by numeric id or by topic (case-insensitive), or None."""
        ref = str(ref or "").strip()
        row = None
        if ref.isdigit():
            row = self.db.execute(
                f"SELECT {', '.join(_KEYS)} FROM knowledge WHERE id=?",
                (int(ref),)).fetchone()
        if row is None:
            row = self.db.execute(
                f"SELECT {', '.join(_KEYS)} FROM knowledge WHERE topic=?",
                (" ".join(ref.split()),)).fetchone()
        return dict(zip(_KEYS, row)) if row else None

    def delete(self, ref):
        """Delete by id or topic; returns the number of rows removed."""
        rec = self.get(ref)
        if not rec:
            return 0
        cur = self.db.execute("DELETE FROM knowledge WHERE id=?", (rec["id"],))
        self.db.commit()
        return cur.rowcount

    def list_all(self):
        rows = self.db.execute(
            f"SELECT {', '.join(_KEYS)} FROM knowledge ORDER BY updated DESC"
        ).fetchall()
        return [dict(zip(_KEYS, r)) for r in rows]

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]

    # ------------------------------------------------------------ embeddings
    def vectors(self):
        """[(id, embedding blob)] of every note that has a vector."""
        return self.db.execute(
            "SELECT id, embedding FROM knowledge WHERE embedding IS NOT NULL"
        ).fetchall()

    def missing_embedding(self, limit=20):
        """Notes without a vector (saved while the model was down) — oldest first,
        so a later search can backfill them in small batches."""
        rows = self.db.execute(
            f"SELECT {', '.join(_KEYS)} FROM knowledge WHERE embedding IS NULL "
            "ORDER BY updated LIMIT ?", (limit,)).fetchall()
        return [dict(zip(_KEYS, r)) for r in rows]

    def set_embedding(self, note_id, embedding, emb_model):
        self.db.execute("UPDATE knowledge SET embedding=?, emb_model=? WHERE id=?",
                        (embedding, emb_model, note_id))
        self.db.commit()

    def by_ids(self, ids):
        """Note dicts in the given id order (semantic-search ranking)."""
        by_id = {}
        for i in ids:
            row = self.db.execute(
                f"SELECT {', '.join(_KEYS)} FROM knowledge WHERE id=?", (i,)).fetchone()
            if row:
                by_id[i] = dict(zip(_KEYS, row))
        return [by_id[i] for i in ids if i in by_id]

    # ------------------------------------------------------------ fallback
    def search_keyword(self, query, limit=5):
        """Keyword fallback: rank by how many query tokens appear in topic+content."""
        tokens = {t for t in (query or "").lower().split() if len(t) >= 3}
        if not tokens:
            return []
        scored = []
        for rec in self.list_all():
            hay = f"{rec['topic']} {rec['content']}".lower()
            score = sum(1 for t in tokens if t in hay)
            if score:
                scored.append((score, rec))
        scored.sort(key=lambda s: (-s[0], -s[1]["updated"]))
        return [rec for _, rec in scored[:limit]]
