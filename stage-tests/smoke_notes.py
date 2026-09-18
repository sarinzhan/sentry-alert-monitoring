"""Smoke test for the notes memory (run inside the app image, no network).

Covers: schema, KnowledgeRepo CRUD + keyword fallback, KnowledgeService
semantic ranking with a stub embedder (real model download is exercised on
stage, not here), tool server construction, and the full controller import.

    docker run --rm -e DB_PATH=/tmp/t.db -v .../smoke_notes.py:/smoke.py:ro \
        sentry-monit:notes-test python /smoke.py
"""
import asyncio
import os
import sys

os.environ.setdefault("DB_PATH", "/tmp/smoke.db")

from app.db import Database
from app.repositories.knowledge import KnowledgeRepo
from app.services.embeddings import to_blob, from_blob, cosine
from app.services.knowledge_tools import (
    KnowledgeService, build_knowledge_server, NOTES_PROMPT,
)

failures = []


def check(name, cond):
    print(("ok  " if cond else "FAIL") + f"  {name}")
    if not cond:
        failures.append(name)


class StubEmbedder:
    """Deterministic 3-dim 'embeddings': known words map to fixed directions."""
    AXES = {"billing": (1.0, 0.0, 0.0), "logs": (0.0, 1.0, 0.0),
            "gitlab": (0.0, 0.0, 1.0)}

    async def embed(self, texts, kind="passage"):
        out = []
        for t in texts:
            v = [0.01, 0.01, 0.01]
            for w, ax in self.AXES.items():
                if w in t.lower():
                    v = [a + b for a, b in zip(v, ax)]
            out.append(v)
        return out


class DownEmbedder:
    async def embed(self, texts, kind="passage"):
        return None


async def main():
    db = Database()
    repo = KnowledgeRepo(db.conn)

    # --- repo CRUD + upsert ---
    nid, created = repo.save("Billing errors", "look in billing-service logs",
                             source="test")
    check("insert creates", created and nid)
    nid2, created2 = repo.save("billing ERRORS", "updated content", source="test2")
    check("upsert by topic is case-insensitive", nid2 == nid and not created2)
    check("update replaced content",
          repo.get(nid)["content"] == "updated content")
    check("get by topic", repo.get("billing errors")["id"] == nid)
    repo.save("where logs live", "sentry logs dataset, search_logs tool")
    repo.save("gitlab repos map", "GITLAB_PROJECTS maps sentry id to repo")
    check("list_all", repo.count() == 3)
    check("keyword search hits",
          [r["topic"].lower() for r in repo.search_keyword("billing problem")]
          == ["billing errors"])

    # --- semantic search via service (stub embedder) ---
    svc = KnowledgeService(repo, StubEmbedder())
    # notes above were saved without vectors -> first search must backfill
    hits = await svc.search("billing charge failed")
    check("backfill happened", len(repo.vectors()) == 3)
    check("semantic top hit is billing",
          hits and hits[0][0]["topic"].lower() == "billing errors")
    check("semantic scores attached", hits[0][1] is not None)

    # --- keyword fallback when embeddings are down ---
    svc_down = KnowledgeService(repo, DownEmbedder())
    hits = await svc_down.search("billing")
    check("fallback returns keyword hits",
          hits and hits[0][0]["topic"].lower() == "billing errors"
          and hits[0][1] is None)

    # --- save through the service embeds ---
    await svc.save("logs tips", "use search_logs full-text", source="test")
    check("service save stores vector",
          repo.get("logs tips")["emb_model"] is not None)

    # --- blob round-trip / cosine sanity ---
    v = [0.1, -0.5, 3.0]
    check("blob round-trip", [round(x, 4) for x in from_blob(to_blob(v))]
          == [round(x, 4) for x in v])
    check("cosine self is 1", abs(cosine(v, v) - 1.0) < 1e-6)

    # --- tool server builds; tools callable ---
    servers, allowed = build_knowledge_server(svc, source="smoke")
    check("server + allowed tools",
          "notes" in servers and
          set(allowed) == {"mcp__notes__search_notes", "mcp__notes__save_note"})
    check("NOTES_PROMPT mentions both tools",
          "search_notes" in NOTES_PROMPT and "save_note" in NOTES_PROMPT)

    # --- delete ---
    check("delete by id", repo.delete(nid) == 1 and repo.get(nid) is None)

    # --- web chat: conversations + messages (repositories.chats) ---
    from app.repositories.chats import ChatsRepo
    chats = ChatsRepo(db.conn)
    cid = chats.create("alice", "почему падает биллинг?")
    check("chat created", chats.get(cid, "alice")["title"].startswith("почему"))
    check("chat scoped by user", chats.get(cid, "bob") is None)
    chats.add_message(cid, "user", "почему падает биллинг?")
    chats.add_message(cid, "assistant", "смотрю…", llm_id="llm_x1")
    msgs = chats.messages(cid)
    check("messages in order", [m["role"] for m in msgs] == ["user", "assistant"]
          and msgs[1]["llm_id"] == "llm_x1")
    chats.set_session(cid, "sess-abc")
    check("session id stored", chats.get(cid, "alice")["session_id"] == "sess-abc")
    lst = chats.list_for("alice")
    check("list_for counts messages", lst[0]["id"] == cid and lst[0]["messages"] == 2)
    check("delete scoped", chats.delete(cid, "bob") == 0)
    check("delete works", chats.delete(cid, "alice") == 1
          and chats.messages(cid) == [])

    # --- usage stats: per-day split shared vs personal token ---
    from app.repositories.web_requests import WebRequestsRepo

    class FakeRec:
        text, in_tokens, out_tokens, cost, duration_ms = "ans", 100, 20, 0.01, 5

    wr = WebRequestsRepo(db.conn)
    wr.add({"description": "q1"}, rec=FakeRec(), llm_id="llm_a", username="alice")
    wr.add({"description": "q2"}, rec=FakeRec(), llm_id="llm_b",
           username="alice", own_token=True)
    wr.add({"description": "q3"}, error="boom", username="alice")
    wr.add({"description": "q4"}, rec=FakeRec(), llm_id="llm_c", username="bob")
    days = wr.usage_by_day("alice", 0, tz_offset_hours=6)
    check("usage: one local-day bucket", len(days) == 1)
    check("usage: shared bucket counts failed run too",
          days[0]["shared"]["requests"] == 2
          and days[0]["shared"]["in_tokens"] == 100)
    check("usage: own bucket separate",
          days[0]["own"]["requests"] == 1
          and days[0]["own"]["out_tokens"] == 20)
    check("usage: scoped by user",
          wr.usage_by_day("bob", 0, 6)[0]["shared"]["requests"] == 1)

    # --- ask() builds a bare prompt on resume (no system ctx duplication) ---
    import inspect
    from app.services.investigation import ask as ask_fn
    check("ask accepts resume", "resume" in inspect.signature(ask_fn).parameters)
    from app.services.llm import LlmCall
    check("LlmCall carries session_id", hasattr(LlmCall(), "session_id"))

    # --- the whole app still imports (wiring check) ---
    import app.controller  # noqa: F401
    check("app.controller imports", True)

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("all smoke checks passed")


asyncio.run(main())
