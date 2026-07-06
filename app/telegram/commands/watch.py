"""/watch /watched — keyword force-send list (global or per-project)."""
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of, actor


async def _list_keywords(update, ctx):
    rows = deps_of(ctx).keywords.list_all()
    if not rows:
        return await reply(update, "Список ключевых слов пуст.")
    body = "\n".join(f"• <code>{esc(t)}</code>" + (f" [{esc(pr)}]" if pr else " [все]")
                     for t, pr in rows)
    await reply(update, "<b>Ключевые слова (force-send):</b>\n" + body)


async def on_watched(update, ctx):
    await _list_keywords(update, ctx)


async def on_watch(update, ctx):
    by = actor(update)
    keywords = deps_of(ctx).keywords
    sub = ctx.args[0].lower() if ctx.args else ""
    if sub == "list":
        return await _list_keywords(update, ctx)
    if sub in ("add", "del") and len(ctx.args) >= 2:
        text = ctx.args[1]
        project = ctx.args[2] if len(ctx.args) >= 3 else None
        scope = f"[{esc(project)}]" if project else "[все проекты]"
        if sub == "add":
            ok = keywords.add(text, project, by)
            msg = (f"Добавлено ключевое слово <code>{esc(text)}</code> {scope}." if ok
                   else f"Уже есть: <code>{esc(text)}</code> {scope}.")
        else:
            n = keywords.delete(text, project)
            msg = (f"Удалено <code>{esc(text)}</code> {scope}." if n
                   else f"Не найдено: <code>{esc(text)}</code> {scope}.")
        return await reply(update, msg)
    await reply(update, "Использование: <code>/watch add &lt;текст&gt; [проект]</code> · "
                        "<code>/watch del &lt;текст&gt; [проект]</code> · <code>/watch list</code>")
