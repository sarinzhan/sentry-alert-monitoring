"""Embedder — multilingual sentence embeddings for the notes memory.

fastembed (ONNX, no torch) with a small multilingual model, loaded lazily in a
worker thread on first use, so startup stays fast and the app runs fine when
the model can't be had: embed() then returns None and the notes search falls
back to keyword matching (knowledge_tools.KnowledgeService). The model is
downloaded from HuggingFace once, through the corporate proxy, and cached
under EMBED_CACHE (next to the SQLite db — the persistent volume).
"""
import os
import asyncio
from array import array

from app.config import (
    EMBED_MODEL, EMBED_CACHE, ANTHROPIC_CA_BUNDLE, ANTHROPIC_SSL_INSECURE, log,
)


def to_blob(vec):
    """float vector -> bytes for the knowledge.embedding BLOB column."""
    return array("f", vec).tobytes()


def from_blob(blob):
    v = array("f")
    v.frombytes(blob)
    return v


def cosine(a, b):
    """Cosine similarity of two vectors (0 when either is degenerate)."""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na ** 0.5 * nb ** 0.5)


class Embedder:
    """Lazy fastembed wrapper. embed() returns vectors or None — never raises."""

    def __init__(self):
        self._model = None
        self._failed = False
        self._lock = asyncio.Lock()

    def _load(self):
        # The HF download goes through the MITM proxy: trust the corporate CA,
        # or (last resort) disable verification — CURL_CA_BUNDLE="" is the
        # documented requests/huggingface_hub switch. These are requests-only
        # vars; the app's own httpx/SDK clients don't read them.
        if ANTHROPIC_CA_BUNDLE:
            os.environ.setdefault("REQUESTS_CA_BUNDLE", ANTHROPIC_CA_BUNDLE)
        elif ANTHROPIC_SSL_INSECURE:
            os.environ.setdefault("CURL_CA_BUNDLE", "")
        from fastembed import TextEmbedding
        return TextEmbedding(model_name=EMBED_MODEL, cache_dir=EMBED_CACHE)

    async def embed(self, texts, kind="passage"):
        """Vectors (list of float lists) for texts, or None when embeddings
        are unavailable (fastembed not installed / model download failed).
        kind is 'query' or 'passage' — the e5 model family expects the input
        prefixed with which side of the search it is; other models ignore it."""
        if self._failed or not texts:
            return None
        async with self._lock:
            if self._model is None:
                try:
                    self._model = await asyncio.to_thread(self._load)
                    log.info("embeddings ready: %s", EMBED_MODEL)
                except Exception as e:
                    self._failed = True
                    log.warning("embeddings unavailable (%s) — notes search "
                                "falls back to keyword matching", e)
                    return None
            prefixed = ([f"{kind}: {t}" for t in texts]
                        if "e5" in EMBED_MODEL.lower() else list(texts))
            try:
                vecs = await asyncio.to_thread(
                    lambda: list(self._model.embed(prefixed)))
            except Exception as e:
                log.warning("embed failed: %s", e)
                return None
        return [[float(x) for x in v] for v in vecs]