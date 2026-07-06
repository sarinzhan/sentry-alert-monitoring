"""/status <id> — issue state: counts and last alert."""
from app.utils import esc
from app.summaries import fmt_duration
from app.telegram.formatting import fmt_ts
from app.telegram.commands._helpers import reply, chat_of, deps_of


async def on_status(update, ctx):
    if not ctx.args:
        return await reply(update, "Использование: <code>/status &lt;id&gt;</code>")
    chat_id, _ = chat_of(update)
    deps = deps_of(ctx)
    windows = deps.rules.effective(chat_id)["stat_windows"]
    info = deps.issues.issue_status(ctx.args[0], windows)
    if not info:
        return await reply(update, f"Ошибка <code>{esc(ctx.args[0])}</code> не найдена.")
    labels = "/".join(fmt_duration(w) for w in windows)
    counts = "/".join(str(c) for c in info["counts"])
    title = esc(info.get("title") or "?")
    title = f'<a href="{esc(info["url"])}">{title}</a>' if info.get("url") else title
    lines = [
        f"#<code>{esc(info['short'])}</code> · <b>{esc(info.get('project') or '?')}</b>",
        title,
        f"события ({labels}): <b>{counts}</b>",
        f"последний алерт: {fmt_ts(info.get('last_sent'))}",
    ]
    await reply(update, "\n".join(lines))
