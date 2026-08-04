"""/req <request_id> [период] — все события одного запроса по всем сервисам.

Ищет через Sentry Discover по ключам из SENTRY_REQUEST_ID_FIELDS (по очереди,
пока один не даст результат). Период — 24h по умолчанию, можно 1h/12h/7d и т.п.
"""
import re

from app.services.sentry_api import discover_error_hint
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of

MAX_LINES = 20
TITLE_MAX = 90
_PERIOD = re.compile(r"^\d+[mhdw]$")

USAGE = ("Использование: <code>/req &lt;request_id&gt; [период]</code>\n"
         "Например: <code>/req 7f3a9c12 24h</code> — период 1h/12h/24h/7d, "
         "по умолчанию 24h.")


def fmt_event(ev, issues):
    """One event line: time · [project] · title · #short (if we know the issue)."""
    ts = (ev.get("timestamp") or "").replace("T", " ")[:19]
    title = (ev.get("title") or ev.get("message") or "?")[:TITLE_MAX]
    line = f"{esc(ts)} · [{esc(ev.get('project') or '?')}] · {esc(title)}"
    info = issues.resolve_ref(str(ev.get("issue.id") or "")) if ev.get("issue.id") else None
    if info:
        line += f" · <code>#{esc(info['short'])}</code>"
    return line


async def on_req(update, ctx):
    deps = deps_of(ctx)
    if not ctx.args:
        return await reply(update, USAGE)
    if not deps.sentry.enabled:
        return await reply(update, "Sentry API не настроен (SENTRY_API_TOKEN).")
    request_id = ctx.args[0].strip()
    period = ctx.args[1].strip() if len(ctx.args) > 1 else "24h"
    if not _PERIOD.match(period):
        return await reply(update, USAGE)

    await reply(update, "🔎 ищу…")
    try:
        key, events = await deps.sentry.events_for_request(request_id, stats_period=period)
    except Exception as e:
        return await reply(update, f"⚠️ Sentry Discover недоступен: {esc(discover_error_hint(e))}")
    if not events:
        from app.config import SENTRY_REQUEST_ID_FIELDS
        return await reply(update,
            f"По <code>{esc(request_id)}</code> за {esc(period)} ничего не найдено "
            f"(искал по: {esc(', '.join(SENTRY_REQUEST_ID_FIELDS))}). "
            "Попробуйте больший период.")

    lines = [f"🔎 <b>{esc(request_id)}</b> · {len(events)} событий за {esc(period)} "
             f"(поле <code>{esc(key)}</code>):"]
    lines += [fmt_event(ev, deps.issues) for ev in events[:MAX_LINES]]
    if len(events) > MAX_LINES:
        lines.append(f"… и ещё {len(events) - MAX_LINES} (полный список: "
                     f"<code>GET /api/request/{esc(request_id)}</code>)")
    await reply(update, "\n".join(lines))
