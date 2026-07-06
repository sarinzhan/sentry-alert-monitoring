"""/set — change this chat's own trigger rules (or /set reset)."""
from app.utils import esc
from app.summaries import params_summary, parse_duration
from app.telegram.commands._helpers import reply, chat_of, deps_of

# /set <name> -> (chat_rules column, value parser). Mirrors the /params lines.
RULE_KEYS = {
    "ongoing":            ("ongoing_sec", "duration"),
    "critical_window":    ("critical_window_sec", "duration"),
    "critical_threshold": ("critical_threshold", "int"),
    "affected_users":     ("affected_user_threshold", "int"),
    "critical_ratelimit": ("critical_ratelimit_sec", "duration"),
    "stat_windows":       ("stat_windows", "windows"),
}


async def on_set(update, ctx):
    chat_id, _ = chat_of(update)
    rules_repo = deps_of(ctx).rules
    args = ctx.args
    if args and args[0].lower() == "reset":
        rules_repo.reset(chat_id)
        return await reply(update, "✅ Правила этого чата сброшены к значениям по умолчанию.")
    if len(args) < 2 or args[0] not in RULE_KEYS:
        keys = ", ".join(f"<code>{esc(k)}</code>" for k in RULE_KEYS)
        return await reply(update, "Использование: <code>/set &lt;параметр&gt; &lt;значение&gt;</code> "
                           f"или <code>/set reset</code>.\nПараметры: {keys}\n"
                           "Примеры: <code>/set ongoing 12h</code> · "
                           "<code>/set critical_threshold 15</code> · "
                           "<code>/set stat_windows 12h/6h/10m</code>")
    column, kind = RULE_KEYS[args[0]]
    raw = args[1]
    if kind == "duration":
        val = parse_duration(raw)
        if val is None or val <= 0:
            return await reply(update, "Нужна длительность, напр. <code>12h</code>, "
                               "<code>10m</code>, <code>30s</code>.")
    elif kind == "int":
        try:
            val = int(raw)
            assert val >= 0
        except (ValueError, AssertionError):
            return await reply(update, "Нужно неотрицательное целое число.")
    else:  # windows: "12h/6h/10m"
        parts = [parse_duration(x) for x in raw.replace(",", "/").split("/") if x.strip()]
        if len(parts) != 3 or any(v is None or v <= 0 for v in parts):
            return await reply(update, "Нужно ровно 3 окна, напр. <code>12h/6h/10m</code>.")
        val = ",".join(str(v) for v in parts)
    rules_repo.set_rule(chat_id, column, val)
    rules = rules_repo.effective(chat_id)
    await reply(update, f"✅ <code>{esc(args[0])}</code> обновлён для этого чата.\n<pre>"
                + esc(params_summary(rules)) + "</pre>")
