"""/api <путь> — документация API-эндпоинта сервиса, сгенерированная из кода.

Агентный LLM ищет эндпоинт в замапленных GitLab-репозиториях: регистр не важен,
опечатки и частичные пути допустимы. Если однозначного совпадения нет — вместо
догадок возвращает список ближайших эндпоинтов. Готовый документ кешируется по
запросу; /api refresh <путь> перегенерирует его.
"""
from app.config import GITLAB_PROJECTS, GITLAB_REF, ENABLE_LLM_TOOLS
from app.services.gitlab_tools import build_gitlab_server
from app.utils import esc
from app.telegram.commands._helpers import reply, deps_of, llm_cost_line

ANSWER_MAX = 3600

USAGE = ("Использование: <code>/api &lt;путь или описание эндпоинта&gt;</code>\n"
         "Например: <code>/api products/connect</code> или "
         "<code>/api подключение продукта</code>.\n"
         "<code>/api refresh &lt;путь&gt;</code> — перегенерировать документ.")

PROMPT = (
    "You are a senior backend engineer writing API documentation for internal "
    "company services. The user asks about an HTTP endpoint: '{q}'. The query "
    "may be a partial path, differently cased, mistyped, or a short description.\n\n"
    "You have read-only GitLab tools (search_code, find_file, read_file, blame, "
    "commit_diff, recent_commits) over the mapped service repos: {repos} "
    "(ref {ref}; pass the 'repo' argument to target a specific one). Locate the "
    "endpoint by searching controller route mappings (@RequestMapping, "
    "@GetMapping, @PostMapping, @PutMapping, @DeleteMapping, or equivalent "
    "route definitions) — match case-insensitively and try likely spelling "
    "variants of the query.\n\n"
    "If EXACTLY ONE endpoint matches, read its handler (and the services it "
    "calls) and reply with:\n"
    "METHOD /full/path — one-line summary\n"
    "Контракт: request params/body fields (types, required or not), response "
    "shape, error responses (HTTP codes / exceptions and when they happen).\n"
    "curl: a complete curl example with realistic values.\n"
    "Поведение: validations, business rules, side effects (DB writes, calls to "
    "other services, events) — taken from the actual handler code.\n\n"
    "If ZERO or SEVERAL endpoints plausibly match, DO NOT guess: reply with a "
    "short numbered list of the closest endpoints (METHOD /path — что делает, "
    "какой сервис), so the user can re-run /api with an exact path.\n\n"
    "Reply in Russian (identifiers/paths in English), plain text without "
    "markdown, at most 3000 characters."
)


async def on_api(update, ctx):
    deps = deps_of(ctx)
    args = list(ctx.args or [])
    refresh = bool(args) and args[0].lower() == "refresh"
    if refresh:
        args = args[1:]
    if not args:
        return await reply(update, USAGE)
    if not (deps.llm.enabled and ENABLE_LLM_TOOLS):
        return await reply(update, "LLM выключен (ENABLE_LLM / ENABLE_LLM_TOOLS).")
    if not (deps.gitlab.enabled and GITLAB_PROJECTS):
        return await reply(update, "GitLab не настроен (GITLAB_URL/TOKEN/PROJECTS).")

    q = " ".join(args).strip()
    q_norm = " ".join(q.lower().split())
    if not refresh:
        row = deps.context.get_api_doc(q_norm)
        if row:
            doc, llm_id, _ = row
            tail = f"\n<i>📄 из кеша · 🔍 <code>/llm {esc(llm_id)}</code></i>" if llm_id \
                else "\n<i>📄 из кеша</i>"
            return await reply(update, doc[:ANSWER_MAX] + tail)

    await reply(update, "🤖 ищу эндпоинт в коде…")
    repos = sorted(set(GITLAB_PROJECTS.values()))
    servers, allowed = build_gitlab_server(deps.gitlab, repos[0])
    prompt = PROMPT.format(q=q, repos=", ".join(repos), ref=GITLAB_REF)
    rec = await deps.llm.complete(prompt, mcp_servers=servers, allowed_tools=allowed)
    if rec is None or not rec.text:
        return await reply(update, "Не получилось получить ответ — смотрите логи.")
    llm_id = deps.llm_audit.put("api", rec, chat_id=update.effective_chat.id)
    doc = f"🤖 {esc(rec.text)}"
    deps.context.put_api_doc(q_norm, doc, llm_id)
    await reply(update, f"{doc[:ANSWER_MAX]}\n{llm_cost_line(rec, llm_id)}")
