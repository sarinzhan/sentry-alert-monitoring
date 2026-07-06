"""/subscribe /unsubscribe /subscriptions — this chat's project subscriptions."""
from app.config import ALERT_STATUSES
from app.utils import esc
from app.telegram.commands._helpers import reply, chat_of, deps_of, actor


async def on_subscribe(update, ctx):
    by = actor(update)
    if not ctx.args:
        return await reply(update, "Использование: <code>/subscribe &lt;проект|all&gt;</code> "
                                   "(id, имя или <code>all</code>). Список: <code>/projects</code>")
    chat_id, thread_id = chat_of(update)
    subs = deps_of(ctx).subscriptions
    done = []
    for a in ctx.args:
        proj = "*" if a.lower() in ("all", "*", "все") else a
        if subs.subscribe(chat_id, proj, thread_id, by):
            done.append("все проекты" if proj == "*" else proj)
    where = " (тема этой ветки)" if thread_id else ""
    await reply(update, f"✅ Подписка добавлена: <b>{esc(', '.join(done))}</b>{where}. "
                        f"Статусы: используйте <code>/alerts</code>, правила: <code>/params</code>.")


async def on_unsubscribe(update, ctx):
    if not ctx.args:
        return await reply(update, "Использование: <code>/unsubscribe &lt;проект|all&gt;</code>")
    chat_id, _ = chat_of(update)
    subs = deps_of(ctx).subscriptions
    n = 0
    for a in ctx.args:
        proj = "*" if a.lower() in ("all", "*", "все") else a
        n += subs.unsubscribe(chat_id, proj)
    await reply(update, f"Отписка: удалено записей — {n}." if n else "Таких подписок не найдено.")


async def on_subscriptions(update, ctx):
    chat_id, _ = chat_of(update)
    deps = deps_of(ctx)
    subs = deps.subscriptions.list_for(chat_id)
    if not subs:
        return await reply(update, "Этот чат не подписан ни на один проект. "
                                   "<code>/subscribe &lt;проект|all&gt;</code>")
    body = "\n".join("• все проекты" if s == "*" else f"• <code>{esc(s)}</code>" for s in subs)
    rules = deps.rules.effective(chat_id)
    st = "all" if not rules.get("statuses") else "/".join(
        s for s in ALERT_STATUSES if s in rules["statuses"])
    await reply(update, f"<b>Подписки этого чата:</b>\n{body}\n\nСтатусы: <b>{esc(st)}</b>")
