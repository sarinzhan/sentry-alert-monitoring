"""/start — welcome the chat and point it at /subscribe."""
from app.config import log
from app.utils import esc
from app.telegram.commands._helpers import reply, chat_of


async def on_start(update, ctx):
    """Alerts are opt-in per chat: nothing is sent until the chat subscribes."""
    chat_id, _ = chat_of(update)
    lines = [
        "👋 <b>Sentry alerts bot.</b> Этот чат пока не получает алерты.",
        "",
        "1) <code>/subscribe &lt;проект|all&gt;</code> — подписаться (см. <code>/projects</code>)",
        "2) <code>/alerts new ongoing escalating|all</code> — какие статусы (по умолч. все)",
        "3) <code>/set &lt;параметр&gt; &lt;значение&gt;</code> — правила чата (<code>/params</code>)",
        "",
        "Подписки этого чата: <code>/subscriptions</code> · все команды: <code>/help</code>",
        f"<i>chat id:</i> <code>{esc(chat_id)}</code>",
    ]
    await reply(update, "\n".join(lines))
    log.info("/start chat_id=%s", chat_id)
