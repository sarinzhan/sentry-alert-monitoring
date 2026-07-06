"""/projects — the configured project catalog, marking this chat's subscriptions."""
from app.utils import esc
from app.telegram.commands._helpers import reply, chat_of, deps_of


async def on_projects(update, ctx):
    deps = deps_of(ctx)
    rows = deps.subscriptions.projects_overview()
    if not rows:
        return await reply(update, "Проекты не сконфигурированы "
                                   "(SENTRY_PROJECTS / GITLAB_PROJECTS).")
    chat_id, _ = chat_of(update)
    subs = set(deps.subscriptions.list_for(chat_id))
    all_sub = "*" in subs
    lines = ["<b>Проекты</b> (✅ = этот чат подписан):"]
    for r in rows:
        name = esc(r["name"] or "?")
        repo = f" → <code>{esc(r['repo'])}</code>" if r["repo"] else ""
        mark = "✅ " if (all_sub or str(r["id"]) in subs or (r["name"] and r["name"] in subs)) else ""
        lines.append(f"{mark}<code>{esc(r['id'])}</code> {name}{repo}")
    lines.append("\nПодписаться: <code>/subscribe &lt;id|имя|all&gt;</code>")
    await reply(update, "\n".join(lines))
