"""/start — beginner-friendly onboarding: what the bot does, alert types, parameters."""
from app.utils import esc
from app.summaries import fmt_duration
from app.telegram.commands._helpers import reply, chat_of, deps_of


async def on_start(update, ctx):
    """Explain the bot from scratch: what it does, the alert types, the parameters,
    and the 3 steps to start. Uses this chat's current values so examples are real."""
    chat_id, _ = chat_of(update)
    r = deps_of(ctx).rules.effective(chat_id)          # this chat's effective rules
    windows = "/".join(fmt_duration(w) for w in r["stat_windows"])

    lines = [
        "👋 <b>Привет! Я приношу ошибки из Sentry прямо в этот чат.</b>",
        "",
        "<b>Sentry</b> — это система, которая ловит ошибки в приложениях (упало, "
        "выбросило исключение и т.п.). Я слежу за ними и присылаю сюда <b>только те, "
        "что важны именно вам</b>. По умолчанию я молчу, пока вы не подпишетесь.",
        "",
        "<b>📥 Какие бывают алерты (типы ошибок)</b>",
        "🆕 <b>new</b> — ошибку увидели <b>впервые</b>.",
        "🔁 <b>ongoing</b> — ошибка <b>всё ещё происходит</b>. Я не спамлю: напоминаю "
        "не чаще, чем раз в «min gap».",
        "🚨 <b>escalating</b> — <b>резкий всплеск</b> за короткое время: слишком много "
        "ошибок ИЛИ слишком много затронутых пользователей.",
        "",
        "<b>⚙️ Параметры</b> — по ним я решаю, что «важно». У каждого чата свои. "
        "Изменить — <code>/set</code>, посмотреть — <code>/params</code>. Сейчас:",
        f"• <b>ongoing (min gap)</b> — как часто напоминать про одну и ту же ошибку "
        f"(сейчас <b>{esc(fmt_duration(r['ongoing_sec']))}</b>).",
        f"• <b>critical window</b> — короткое окно, в котором ловим всплеск "
        f"(сейчас <b>{esc(fmt_duration(r['critical_window_sec']))}</b>).",
        f"• <b>critical error threshold</b> — сколько ошибок в этом окне = 🚨 "
        f"(сейчас <b>&gt;{esc(r['critical_threshold'])}</b>).",
        f"• <b>affected user threshold</b> — сколько <b>разных</b> пользователей задето = 🚨 "
        f"(сейчас <b>&gt;={esc(r['affected_user_threshold'])}</b>).",
        f"• <b>critical rate limit</b> — как часто максимум слать 🚨 "
        f"(сейчас <b>1 раз в {esc(fmt_duration(r['critical_ratelimit_sec']))}</b>).",
        f"• <b>stat windows</b> — 3 периода для счётчиков во 2-й строке алерта "
        f"(сейчас <b>{esc(windows)}</b>, напр. 2343/43/22).",
        "",
        "<b>🚀 Как начать за 3 шага</b>",
        "1) <code>/subscribe &lt;проект|all&gt;</code> — выбрать проекты (список: <code>/projects</code>).",
        "2) <code>/alerts new ongoing escalating|all</code> — какие типы получать (по умолч. все).",
        "3) <code>/set ongoing 12h</code> и т.п. — подстроить правила (текущие — <code>/params</code>).",
        "",
        "<b>🔎 Ещё команды</b>",
        "<code>/status &lt;id&gt;</code> — детали ошибки · <code>/ai &lt;id&gt;</code> — AI-разбор "
        "причины и фикса · <code>/watch</code> — слать по ключевому слову · "
        "<code>/map</code> — автор коммита → @telegram · <code>/help</code> — все команды.",
        "",
        "<i>id — это короткий код <code>#abcdef</code> из 2-й строки любого алерта.</i>",
        f"<i>chat id этого чата: <code>{esc(chat_id)}</code></i>",
    ]
    await reply(update, "\n".join(lines))
