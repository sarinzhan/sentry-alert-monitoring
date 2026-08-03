"""build_message — render one parsed event into the Telegram HTML alert."""
from app.config import STAT_WINDOWS
from app.summaries import fmt_duration
from app.services.llm import money
from app.utils import esc

# status -> emoji for the message header
STATUS_EMOJI = {"new": "🆕", "ongoing": "🔁", "escalating": "🚨"}

# e.g. "12h/6h/10m" — the default period labels shown next to the line-2 counts
STAT_LABELS = "/".join(fmt_duration(w) for w in STAT_WINDOWS)


def build_message(p: dict, analysis: str = None) -> str:
    status = p.get("status") or "ongoing"
    emoji = STATUS_EMOJI.get(status, "🔴")
    project = esc(p.get("project") or "sentry")
    env = esc(p.get("environment") or "?")

    # line 1: <emoji> project · env · status
    lines = [f"{emoji} <b>{project}</b> · {env} · {esc(status)}"]
    # line 2: <@telegram or vcs author> · <commit date-time> · counts (periods) · #<short>
    nums = "/".join(esc(c) for c in (p.get("counts") or []))
    labels = p.get("stat_labels") or STAT_LABELS
    counts = f"{nums} ({labels})" if nums else ""
    b = p.get("blame") or {}
    who = f"@{esc(b['tg'])}" if b.get("tg") else (esc(b["author"]) if b.get("author") else "")
    author = f"{who} · {esc(b.get('date') or '?')}" if who else ""
    short = f"<code>#{esc(p.get('short'))}</code>" if p.get("short") else ""
    lines.append(" · ".join(x for x in (author, counts, short) if x))
    # line 3: the commit message that last touched the crash line
    if b.get("subject"):
        lines.append(f"💬 <i>{esc(b['subject'])}</i>")
    lines.append("")

    # LLM cause / fix first (only present for escalating prod alerts)
    if analysis:
        lines.append(analysis)
        lines.append("")

    # then the rest
    lines.append(f"<b>{esc(p.get('title'))}</b>")
    if p.get("value") and p.get("value") != p.get("title"):
        lines.append(f"<code>{esc(p['value'])}</code>")
    lines.append("")

    if p.get("culprit"):
        lines.append(f"<b>Culprit:</b> <code>{esc(p['culprit'])}</code>")

    meta = []
    if p.get("level"):      meta.append(f"level {esc(p['level'])}")
    if p.get("count"):      meta.append(f"events {esc(p['count'])}")
    if p.get("user_count"): meta.append(f"users {esc(p['user_count'])}")
    if meta:
        lines.append(" · ".join(meta))

    if p.get("frames"):
        lines.append("<pre>" + "\n".join(esc(f) for f in p["frames"]) + "</pre>")

    if p.get("url"):
        lines += ["", f'<a href="{esc(p["url"])}">Open in Sentry →</a>']

    # copyable quick commands (tap to copy on mobile)
    if p.get("short"):
        s = esc(p["short"])
        lines.append(f"<code>/status {s}</code>  <code>/ai {s}</code>")

    # bottom: LLM cost (USD + som with an api key; tokens only on subscription),
    # or "cached" (with what it saved) on a hit
    m = p.get("llm_meta")
    if m:
        if m.get("cached"):
            saved = f" (saved ~{money(m['cost'])})" if m.get("cost") else ""
            lines.append(f"<i>💰 LLM: cached{esc(saved)}</i>")
        else:
            toks = f"{esc(m.get('in', 0))} in / {esc(m.get('out', 0))} out"
            if m.get("cost"):
                lines.append(f"<i>💰 LLM: {money(m['cost'])} · {toks}</i>")
            else:
                lines.append(f"<i>💰 LLM: {toks}</i>")

    return "\n".join(lines)
