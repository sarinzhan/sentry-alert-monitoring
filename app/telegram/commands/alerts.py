"""/alerts — which statuses (new/ongoing/escalating) this chat receives."""
from app.config import ALERT_STATUSES
from app.utils import esc
from app.telegram.commands._helpers import reply, chat_of, deps_of


async def on_alerts(update, ctx):
    chat_id, _ = chat_of(update)
    rules_repo = deps_of(ctx).rules
    if not ctx.args:
        rules = rules_repo.effective(chat_id)
        st = "all" if not rules.get("statuses") else "/".join(
            s for s in ALERT_STATUSES if s in rules["statuses"])
        return await reply(update, f"Текущие статусы: <b>{esc(st)}</b>\n"
                           "Изменить: <code>/alerts new ongoing escalating</code> или "
                           "<code>/alerts all</code>.")
    toks = [a.lower() for a in ctx.args]
    if "all" in toks:
        rules_repo.set_statuses(chat_id, None)
        return await reply(update, "✅ Этот чат получает <b>все</b> статусы.")
    picked = {t for t in toks if t in ALERT_STATUSES}
    if not picked:
        return await reply(update, "Допустимо: <code>new</code>, <code>ongoing</code>, "
                           "<code>escalating</code> или <code>all</code>.")
    rules_repo.set_statuses(chat_id, picked)
    await reply(update, "✅ Статусы этого чата: <b>"
                + esc("/".join(s for s in ALERT_STATUSES if s in picked)) + "</b>.")
