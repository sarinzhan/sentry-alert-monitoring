"""/params — this chat's effective trigger parameters."""
from app.utils import esc
from app.summaries import params_summary
from app.telegram.commands._helpers import reply, chat_of, deps_of


async def on_params(update, ctx):
    chat_id, _ = chat_of(update)
    rules = deps_of(ctx).rules.effective(chat_id)
    await reply(update, "<b>Параметры этого чата</b>\n<pre>" + esc(params_summary(rules)) + "</pre>")
