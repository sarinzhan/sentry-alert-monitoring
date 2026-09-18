"""Notes tool set for the LLM — a persistent navigation memory.

Two tools over the knowledge table (search_notes / save_note), attached to
every agentic flow (web explain/ask, /ai, /why) next to the GitLab and Sentry
servers. The notes are NAVIGATION hints — how to find things in this system:
which service logs what, where an endpoint lives, config quirks — written by
the model at the end of an investigation and searched at the start of the next
one. Callers append NOTES_PROMPT so the model knows the memory exists.

Search is semantic (multilingual embeddings, brute-force cosine — the table
holds index cards, not documents) and degrades to keyword matching while the
embedding model is unavailable (services.embeddings).
"""
from claude_agent_sdk import tool, create_sdk_mcp_server

from app.config import EMBED_MODEL, log
from app.services.embeddings import to_blob, from_blob, cosine

MAX_TOPIC = 80         # chars; a note is an index card, the topic its label
MAX_CONTENT = 2000     # chars per note
MAX_HITS = 5           # search_notes results

NOTES_PROMPT = (
    "\n\nYou also have a persistent NOTES memory shared across investigations "
    "(search_notes / save_note). Notes are navigation hints left by past "
    "investigations — where to look, not verified facts. START by calling "
    "search_notes with the key terms of the problem. At the END, if you "
    "learned something durable about how to find things in this system "
    "(which service logs what, where an endpoint lives, a config quirk, a "
    "business rule — NOT specifics of this incident or user), call save_note; "
    "reuse an existing topic to update it."
)


class KnowledgeService:
    """Search/save over KnowledgeRepo + Embedder: semantic when the model is
    up (backfilling vectors for notes saved while it was down), keyword
    fallback otherwise. Constructed once in the controller, shared by the
    tool server and the /notes command."""

    def __init__(self, repo, embedder):
        self.repo = repo
        self.embedder = embedder

    async def _backfill(self):
        """Embed notes that were saved while the model was unavailable, a small
        batch per search, so they join the semantic index over time."""
        missing = self.repo.missing_embedding()
        if not missing:
            return
        vecs = await self.embedder.embed(
            [f"{r['topic']}\n{r['content']}" for r in missing], kind="passage")
        if not vecs:
            return
        for rec, vec in zip(missing, vecs):
            self.repo.set_embedding(rec["id"], to_blob(vec), EMBED_MODEL)
        log.info("notes: backfilled %d embeddings", len(missing))

    async def search(self, query, limit=MAX_HITS):
        """[(note dict, score|None)] best-first; score is cosine similarity,
        None for keyword-fallback hits."""
        qvecs = await self.embedder.embed([query], kind="query")
        if qvecs is None:
            return [(rec, None) for rec in self.repo.search_keyword(query, limit)]
        await self._backfill()
        qv = qvecs[0]
        scored = sorted(
            ((cosine(qv, from_blob(blob)), note_id)
             for note_id, blob in self.repo.vectors()), reverse=True)[:limit]
        recs = self.repo.by_ids([note_id for _, note_id in scored])
        return [(rec, score) for (score, _), rec in zip(scored, recs)]

    async def save(self, topic, content, source=None):
        """Upsert one note, embedding it when the model is up. Returns
        (id, created)."""
        vecs = await self.embedder.embed([f"{topic}\n{content}"], kind="passage")
        return self.repo.save(
            topic, content, source=source,
            embedding=to_blob(vecs[0]) if vecs else None,
            emb_model=EMBED_MODEL if vecs else None)


def _text(s: str):
    return {"content": [{"type": "text", "text": s}]}


def build_knowledge_server(knowledge, source=None):
    """(mcp_servers dict, allowed_tools list); knowledge is the shared
    KnowledgeService, source labels save_note writes with the flow that made
    them (explain / ask / ai / why) for the /notes oversight view."""

    @tool(
        "search_notes",
        "Notes memory left by PAST investigations of this same system — "
        "navigation hints: which service logs what, where an endpoint lives, "
        "config quirks, known business rules. Semantic search, Russian or "
        "English. Call this FIRST with the key terms of the problem; treat "
        "hits as hints on where to look, not as verified facts.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "What you are trying to find — a short "
                                         "phrase, e.g. 'balance top-up errors'"},
            },
            "required": ["query"],
        },
    )
    async def search_notes(args):
        query = (args.get("query") or "").strip()
        if not query:
            return _text("query must be non-empty")
        try:
            hits = await knowledge.search(query)
        except Exception as e:
            return _text(f"Error searching notes: {e}")
        log.info("notes tool search q=%r hits=%d", query[:80], len(hits))
        if not hits:
            return _text("No notes yet — nothing has been saved about this. "
                         "Investigate with the other tools; consider saving "
                         "what you learn with save_note.")
        parts = []
        for rec, score in hits:
            tag = f"{score:.2f}" if score is not None else "kw"
            parts.append(f"[{tag}] {rec['topic']}\n{rec['content']}")
        return _text("\n---\n".join(parts))

    @tool(
        "save_note",
        "Save (or update) one note for FUTURE investigations. Only durable "
        "navigation knowledge about the system — which service / log / repo / "
        "field to look at for what kind of problem — NOT facts about the "
        "current user or incident. Saving an existing topic updates that note: "
        f"prefer that over near-duplicate topics. Keep it short (topic up to "
        f"{MAX_TOPIC} chars, content up to {MAX_CONTENT}).",
        {
            "type": "object",
            "properties": {
                "topic": {"type": "string",
                          "description": "Short label, e.g. 'where SIM-exchange "
                                         "failures are logged'"},
                "content": {"type": "string",
                            "description": "The navigation hint itself"},
            },
            "required": ["topic", "content"],
        },
    )
    async def save_note(args):
        topic = " ".join((args.get("topic") or "").split())[:MAX_TOPIC]
        content = (args.get("content") or "").strip()[:MAX_CONTENT]
        if not topic or not content:
            return _text("topic and content must both be non-empty")
        try:
            note_id, created = await knowledge.save(topic, content, source=source)
        except Exception as e:
            return _text(f"Error saving note: {e}")
        log.info("notes tool save id=%s created=%s topic=%r source=%s",
                 note_id, created, topic, source)
        return _text(f"{'Saved' if created else 'Updated'} note {note_id}: {topic}")

    tools = [search_notes, save_note]
    server = create_sdk_mcp_server(name="notes", version="1.0.0", tools=tools)
    return {"notes": server}, [f"mcp__notes__{t.name}" for t in tools]
