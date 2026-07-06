"""/map — map a VCS commit author to a Telegram handle (@mention in alerts)."""
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of, actor


async def on_map(update, ctx):
    by = actor(update)
    usermap = deps_of(ctx).usermap
    args = ctx.args
    sub = args[0].lower() if args else "list"
    if sub == "list":
        rows = usermap.list_all()
        if not rows:
            return await reply(update, "Маппинг пуст. "
                                       "<code>/map &lt;vcs_author&gt; @&lt;tg&gt;</code>")
        body = "\n".join(f"• <code>{esc(v)}</code> → @{esc(tg)}" for v, tg in rows)
        return await reply(update, "<b>VCS-автор → Telegram:</b>\n" + body)
    if sub == "del" and len(args) >= 2:
        n = usermap.delete(args[1])
        return await reply(update, "Удалено." if n else "Не найдено.")
    if sub == "add" and len(args) >= 3:
        vcs, tg = args[1], args[2]
    elif len(args) >= 2 and sub not in ("add", "del"):
        vcs, tg = args[0], args[1]
    else:
        return await reply(update, "Использование: "
                                   "<code>/map &lt;vcs_author&gt; @&lt;tg&gt;</code> · "
                                   "<code>/map del &lt;vcs_author&gt;</code> · "
                                   "<code>/map list</code>")
    if usermap.add(vcs, tg, by):
        return await reply(update, f"<code>{esc(vcs)}</code> → @{esc(tg.lstrip('@'))}")
    await reply(update, "Неверные аргументы.")
