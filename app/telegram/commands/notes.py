"""/notes — the LLM's notes memory: list, show, delete.

The model writes these notes itself at the end of investigations (save_note)
and reads them at the start of the next one, so a wrong note quietly misleads
every future run. This command is the human oversight: inspect what the model
believes about the system and prune what's wrong or stale.
"""
import datetime

from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of

USAGE = ("Использование: <code>/notes</code> — список · "
         "<code>/notes &lt;id&gt;</code> — показать · "
         "<code>/notes del &lt;id&gt;</code> — удалить")


def _when(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


async def on_notes(update, ctx):
    deps = deps_of(ctx)
    if deps.knowledge is None:
        return await reply(update, "Память заметок не подключена.")
    repo = deps.knowledge.repo
    args = list(ctx.args or [])

    if not args:
        rows = repo.list_all()
        if not rows:
            return await reply(update,
                "Заметок пока нет — модель ещё ничего не сохранила.\n" + USAGE)
        body = "\n".join(
            f"{r['id']}. <b>{esc(r['topic'])}</b> — {_when(r['updated'])}"
            f" · {esc(r['source'] or '?')}" for r in rows)
        return await reply(update,
            f"<b>Заметки модели ({len(rows)}):</b>\n{body}\n\n{USAGE}")

    if args[0].lower() in ("del", "delete", "rm"):
        if len(args) < 2:
            return await reply(update, USAGE)
        ref = " ".join(args[1:])
        rec = repo.get(ref)
        if not rec:
            return await reply(update, f"Заметка не найдена: <code>{esc(ref)}</code>")
        repo.delete(rec["id"])
        return await reply(update,
            f"Удалена заметка {rec['id']}: <b>{esc(rec['topic'])}</b>")

    ref = " ".join(args)
    rec = repo.get(ref)
    if not rec:
        return await reply(update, f"Заметка не найдена: <code>{esc(ref)}</code>\n{USAGE}")
    vec = "есть" if rec.get("emb_model") else "нет (поиск по словам)"
    return await reply(update,
        f"<b>{esc(rec['topic'])}</b>\n{esc(rec['content'])}\n"
        f"<i>id {rec['id']} · источник: {esc(rec['source'] or '?')} · "
        f"обновлено {_when(rec['updated'])} · вектор: {vec}</i>")
