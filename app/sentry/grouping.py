"""signature — a normalized key for the deterministic fast-path of incident grouping.

Two issues with the SAME signature are certainly the same bucket (merge without
asking the LLM). A different signature does NOT mean different root cause — the
Grouper falls back to the LLM for that — so the signature only needs to collapse
the obvious repeats (same exception + same crash site, varying ids/numbers).
"""
import re

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{8,}\b", re.I)
_NUM = re.compile(r"\d+")


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = _UUID.sub("#", s)
    s = _HEX.sub("#", s)
    s = _NUM.sub("#", s)
    return s


def signature(p: dict) -> str:
    """Stable `type|culprit` key with volatile ids/numbers/hex normalized out."""
    typ = _norm(p.get("type") or p.get("title") or "")
    culprit = _norm(p.get("culprit") or "")
    return f"{typ}|{culprit}"
