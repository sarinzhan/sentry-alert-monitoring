"""build_message — render one parsed event into the Telegram HTML alert."""
import re

from app.config import STAT_WINDOWS
from app.summaries import fmt_duration
from app.services.llm import money
from app.utils import esc

# status -> emoji for the message header
STATUS_EMOJI = {"new": "🆕", "ongoing": "🔁", "escalating": "🚨"}

# e.g. "12h/6h/10m" — the default period labels shown next to the line-2 counts
STAT_LABELS = "/".join(fmt_duration(w) for w in STAT_WINDOWS)

# how many grouped issues to list inside an incident message
INCIDENT_RELATED_MAX = 10

_DETAIL = re.compile(r"detail='([^']+)'")


def title_from_analysis(analysis: str, limit: int = 120) -> str:
    """A human incident title taken from the LLM analysis — the «Что сломалось: …»
    line (user-facing impact). Returns '' when not found (caller keeps its
    fallback). Plain text; caller escapes."""
    if not analysis:
        return ""
    for line in analysis.splitlines():
        s = line.strip().lstrip("🤖").strip()
        if s.lower().startswith("что сломалось"):
            _, _, rest = s.partition(":")
            rest = rest.strip()
            if rest:
                return rest if len(rest) <= limit else rest[:limit] + "…"
    return ""


def readable_title(t: str, limit: int = 160) -> str:
    """Sentry titles are often a giant exception dump. Pull out the human part —
    the error class + its detail='…' message when present, else the first line,
    truncated. Plain text (caller escapes)."""
    t = (t or "").strip()
    if not t:
        return ""
    m = _DETAIL.search(t)
    if m:
        cls = re.split(r"[:{(]", t, 1)[0].strip()
        return f"{cls}: {m.group(1)}" if cls else m.group(1)
    head = t.splitlines()[0].strip()
    return head if len(head) <= limit else head[:limit] + "…"


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
    # affected users — right under the counts line
    if p.get("user_count"):
        lines.append(f"👥 затронуто пользователей: {esc(p['user_count'])}")
    # LLM explanation — что сломалось / причина / исправление (on every alert)
    if analysis:
        lines += ["", analysis]
    # the commit message that last touched the crash line
    if b.get("subject"):
        lines.append(f"💬 <i>{esc(b['subject'])}</i>")
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
            line = f"💰 LLM: cached{esc(saved)}"
        else:
            toks = f"{esc(m.get('in', 0))} in / {esc(m.get('out', 0))} out"
            line = f"💰 LLM: {money(m['cost'])} · {toks}" if m.get("cost") \
                else f"💰 LLM: {toks}"
        if m.get("llm_id"):
            line += f" · 🔍 <code>/llm {esc(m['llm_id'])}</code>"
        lines.append(f"<i>{line}</i>")

    return "\n".join(lines)


def build_incident_message(inc, members, analysis=None, events=None, users=None,
                           url=None, escalation=False) -> str:
    """The single incident alert (one per incident per chat), edited in place as it
    grows. `analysis` is the reused LLM root-cause description (already HTML-safe)."""
    iid = inc.get("incident_id")
    n = inc.get("member_count") or len(members)
    projects = esc(inc.get("projects") or "?")
    head = "⬆️ <b>Инцидент разрастается</b>" if escalation else "🧩 <b>Инцидент</b>"
    lines = [f"{head} <code>#{esc(iid)}</code> · {projects} · {esc(n)} ошибок"]

    agg = []
    if events is not None:
        agg.append(f"событий: {esc(events)}")
    if users:
        agg.append(f"👥 пользователей: {esc(users)}")
    if agg:
        lines.append(" · ".join(agg))

    if analysis:                      # root-cause description (reused analysis)
        lines += ["", analysis]

    if members:
        lines += ["", "<b>Связанные ошибки:</b>"]
        for m in members[:INCIDENT_RELATED_MAX]:
            short = esc(m.get("short") or "")
            tag = (f'<a href="{esc(m["url"])}">#{short}</a>'
                   if m.get("url") else (f"<code>#{short}</code>" if short else ""))
            title = esc(readable_title(m.get("title"), 120))
            lines.append(f"{tag} {title}".strip())
        if len(members) > INCIDENT_RELATED_MAX:
            lines.append(f"… и ещё {len(members) - INCIDENT_RELATED_MAX}")

    if url:
        lines += ["", f'<a href="{esc(url)}">Open in Sentry →</a>']
    return "\n".join(lines)


def build_incident_resolved(inc, members, now=None) -> str:
    """Short summary posted after an incident is resolved (the live message is
    deleted first). Manual (by @user) or auto (idle timeout)."""
    iid = inc.get("incident_id")
    n = inc.get("member_count") or len(members)
    projects = esc(inc.get("projects") or "?")
    title = esc(title_from_analysis(inc.get("description"))
                or readable_title(inc.get("title")) or "Инцидент")
    by, kind = inc.get("resolved_by"), inc.get("resolved_kind")
    who = f"@{esc(by)}" if by else ("автоматически" if kind == "auto" else "вручную")
    dur = ""
    if inc.get("opened_at") and inc.get("resolved_at"):
        dur = " · " + fmt_duration(int(inc["resolved_at"] - inc["opened_at"]))
    lines = [f"🟢 <b>Инцидент разрешён</b> <code>#{esc(iid)}</code> · {who}{dur}",
             f"{title} · {esc(n)} ошибок · {projects}"]
    if kind == "unverified":
        lines.append("⚠️ без автоматического подтверждения")
    if inc.get("resolution"):
        lines.append(f"<b>Решение:</b> {esc(inc['resolution'])}")
    return "\n".join(lines)
