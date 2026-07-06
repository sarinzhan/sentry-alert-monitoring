"""/ai <id> — ask the LLM for cause/fix on demand."""
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of


async def on_ai(update, ctx):
    if not ctx.args:
        return await reply(update, "Использование: <code>/ai &lt;id&gt;</code>")
    await reply(update, "🤖 анализирую…")
    res = await deps_of(ctx).analysis.analyze_ref(ctx.args[0])
    if res is None:
        return await reply(update, f"Ошибка <code>{esc(ctx.args[0])}</code> не найдена.")
    await reply(update, res)
