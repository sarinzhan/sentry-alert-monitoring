"""Controller — the FastAPI app: HTTP endpoints and the composition root.

The lifespan builds the whole object graph (Database → repositories → services →
pipeline → bot), injects the command dependencies, and starts the bot.

Endpoints:
  GET  /health           liveness check
  POST /webhook          Sentry webhook   -> EventPipeline
  POST /telegram         Telegram webhook -> ChatBotHandler (alternative to polling)
  GET  /web/             investigation form (static page)
  POST /api/investigate  form backend -> Sentry search
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import (ANTHROPIC_MODEL, AUTH_SESSION_HOURS, BOT_TOKEN,
                        LLM_DAILY_LIMIT, LLM_DAILY_TOKENS, TELEGRAM_POLLING,
                        TZ_OFFSET_HOURS, WEB_ENVIRONMENTS, WEB_LLM_MODELS, log)
from app.summaries import banner
from app.db import Database
from app.repositories.issues import IssuesRepo
from app.repositories.subscriptions import SubscriptionsRepo
from app.repositories.rules import RulesRepo
from app.repositories.chat_state import ChatStateRepo
from app.repositories.keywords import KeywordsRepo
from app.repositories.usermap import UserMapRepo
from app.repositories.context import ContextRepo
from app.repositories.llm_audit import LlmAuditRepo
from app.repositories.projects import ProjectsRepo
from app.repositories.web_requests import WebRequestsRepo
from app.repositories.users import UsersRepo, ROLES
from app.repositories.settings import SettingsRepo
from app.repositories.prompts import PromptsRepo, KINDS
from app.services import auth as auth_tokens
from app.services.sentry_api import SentryApiClient
from app.services.gitlab import GitLabClient
from app.services.llm import LlmClient
from app.sentry.decision import Decider
from app.sentry.analysis import AnalysisService
from app.sentry.pipeline import EventPipeline
from app.telegram.bot import ChatBotHandler
from app.telegram.deps import Deps
from app.telegram.commands import register_all, set_bot_commands


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("\n%s", banner())        # effective config first, before we touch the network

    # --- data layer ---
    db = Database()
    issues = IssuesRepo(db.conn)
    subscriptions = SubscriptionsRepo(db.conn)
    rules = RulesRepo(db.conn)
    chat_state = ChatStateRepo(db.conn)
    keywords = KeywordsRepo(db.conn)
    usermap = UserMapRepo(db.conn)
    context = ContextRepo(db.conn)
    llm_audit = LlmAuditRepo(db.conn)
    # seeds from SENTRY_PROJECTS/GITLAB_PROJECTS env, then the DB is the source
    # of truth for the id -> name/gitlab maps (edited in the web UI)
    projects = ProjectsRepo(db.conn)
    web_requests = WebRequestsRepo(db.conn)
    users = UsersRepo(db.conn)            # seeds admin/admin on a fresh DB
    settings = SettingsRepo(db.conn)      # runtime settings (limits, sys prompt)
    prompts = PromptsRepo(db.conn)        # role/problem presets, seeds defaults

    # --- external services ---
    sentry_api = SentryApiClient()
    sentry_api.projects = projects        # auto-register projects it discovers
    gitlab = GitLabClient()
    llm = LlmClient()

    # --- domain ---
    analysis = AnalysisService(issues, context, gitlab, llm, sentry_api, llm_audit)
    decider = Decider(chat_state, issues)

    # --- telegram + pipeline (bot.send is the pipeline's sender) ---
    bot = ChatBotHandler(BOT_TOKEN)
    pipeline = EventPipeline(
        issues=issues, subscriptions=subscriptions, rules=rules, keywords=keywords,
        usermap=usermap, context=context, decider=decider, sentry_api=sentry_api,
        gitlab=gitlab, analysis=analysis, sender=bot.send)

    deps = Deps(issues=issues, subscriptions=subscriptions, rules=rules,
                keywords=keywords, usermap=usermap, analysis=analysis,
                pipeline=pipeline, llm=llm, llm_audit=llm_audit,
                context=context, sentry=sentry_api, gitlab=gitlab)
    register_all(bot.app, deps)

    app.state.bot = bot
    app.state.pipeline = pipeline
    app.state.db = db
    app.state.llm_audit = llm_audit
    app.state.sentry = sentry_api
    app.state.projects = projects
    app.state.web_requests = web_requests
    app.state.users = users
    app.state.settings = settings
    app.state.prompts = prompts
    app.state.services = (sentry_api, gitlab, llm)

    await bot.start(polling=TELEGRAM_POLLING)
    await set_bot_commands(bot.bot)
    log.info("startup complete")
    yield

    await bot.stop()
    for svc in app.state.services:
        await svc.aclose()
    db.close()


app = FastAPI(lifespan=lifespan)


# --- authentication ---------------------------------------------------------
# Every /api/* route (also under the /admin-web alias) requires a logged-in
# user (session cookie), except the login endpoint itself. /health and the
# Telegram/Sentry webhooks stay open — they are machine-to-machine.
# Admin-only areas: user management (/api/users*), the project catalog
# (/api/projects*), runtime settings (/api/settings) and prompt-preset edits
# (writes to /api/prompts — reads are open, the form needs them); a plain
# user gets investigation + history.

_AUTH_OPEN = {"/api/auth/login", "/admin-web/api/auth/login"}
_ADMIN_ONLY = ("/api/users", "/api/projects", "/api/settings")
_SESSION_COOKIE = "session"


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    is_api = path.startswith("/api/") or path.startswith("/admin-web/api/")
    if is_api and path not in _AUTH_OPEN:
        users = getattr(request.app.state, "users", None)
        user = None
        if users is not None:
            token = request.cookies.get(_SESSION_COOKIE)
            username = auth_tokens.parse_token(token, users.secret()) if token else None
            user = users.get(username) if username else None
        if user is None:
            return JSONResponse({"error": "не авторизован"}, status_code=401)
        rel = path[len("/admin-web"):] if path.startswith("/admin-web/") else path
        if user["role"] != "admin" and (
                rel.startswith(_ADMIN_ONLY)
                or (rel.startswith("/api/prompts") and request.method != "GET")):
            return JSONResponse({"error": "нужны права администратора"},
                                status_code=403)
        request.state.user = user
        users.touch(user["username"])      # «последняя активность»
    return await call_next(request)


@app.post("/api/auth/login")
async def auth_login(request: Request):
    """Login with username+password; on success sets the session cookie and
    returns {username, role}."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    users = request.app.state.users
    user = users.verify(body.get("username"), body.get("password"))
    if user is None:
        return JSONResponse({"error": "неверный логин или пароль"},
                            status_code=401)
    resp = JSONResponse({"username": user["username"], "role": user["role"]})
    resp.set_cookie(_SESSION_COOKIE,
                    auth_tokens.make_token(user["username"], users.secret()),
                    httponly=True, samesite="lax", path="/",
                    max_age=int(AUTH_SESSION_HOURS * 3600))
    return resp


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


def _day_start():
    """Unix ts of the local (TZ_OFFSET_HOURS) midnight — the daily quota window."""
    off = TZ_OFFSET_HOURS * 3600
    return (time.time() + off) // 86400 * 86400 - off


def _llm_limits(request):
    """Effective daily limits (requests, tokens) on the shared Claude token:
    the admin-edited values from the settings screen, falling back to the env
    defaults. Semantics: >0 = cap, 0 = shared token forbidden, <0 = unlimited."""
    s = request.app.state.settings
    return (s.get_int("llm_daily_requests", LLM_DAILY_LIMIT),
            s.get_int("llm_daily_tokens", LLM_DAILY_TOKENS))


def _llm_usage_today(request, username):
    """(requests, tokens) the user spent on the shared token since local midnight."""
    wr = request.app.state.web_requests
    day = _day_start()
    return (wr.count_shared_since(username, day),
            wr.tokens_shared_since(username, day))


def _explain_params(request, body):
    """Per-run LLM options from the form: the chosen model (validated against
    WEB_LLM_MODELS), the role preset text (audience) and the admin system
    prompt. Returns (model, audience, system_context, err)."""
    model = (body.get("model") or "").strip() or None
    if model and model not in WEB_LLM_MODELS:
        return None, None, None, JSONResponse(
            {"error": f"модель {model!r} не поддерживается"}, status_code=422)
    audience = None
    role_id = body.get("role_id")
    if role_id:
        preset = request.app.state.prompts.get(role_id)
        if preset is None or preset["kind"] != "role":
            return None, None, None, JSONResponse(
                {"error": "неизвестный шаблон роли"}, status_code=422)
        audience = preset["text"]
    return model, audience, request.app.state.settings.get("system_prompt"), None


def _llm_auth(request):
    """Which Claude token this analysis runs on. Returns (auth_token, own, err):
    the shared token (None) while the user's daily quota lasts, then the
    user's personal token, else a 402 telling the UI to ask for one."""
    user = request.state.user
    req_limit, tok_limit = _llm_limits(request)
    if req_limit < 0 and tok_limit < 0:            # both unlimited
        return None, False, None
    used_req, used_tok = _llm_usage_today(request, user["username"])
    blocked = req_limit == 0 or tok_limit == 0     # shared token forbidden
    over = ((req_limit > 0 and used_req >= req_limit)
            or (tok_limit > 0 and used_tok >= tok_limit))
    if not blocked and not over:
        return None, False, None
    if user["api_token"]:
        return user["api_token"], True, None
    if blocked:
        msg = "Анализы на общем токене отключены администратором."
    else:
        what = (f"{req_limit} анализов"
                if req_limit > 0 and used_req >= req_limit
                else f"{tok_limit} токенов")
        msg = f"Дневной лимит ({what}) на общем токене исчерпан."
    return None, False, JSONResponse(
        {"error": msg + " Добавьте свой Claude токен, чтобы продолжить.",
         "limit_reached": True}, status_code=402)


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """Who am I — the SPA calls this on load to decide login screen vs app.
    Includes the daily LLM quota state so the UI can show what's left."""
    user = request.state.user
    req_limit, tok_limit = _llm_limits(request)
    used_req, used_tok = _llm_usage_today(request, user["username"])
    return {"username": user["username"], "role": user["role"],
            "has_token": bool(user["api_token"]),
            "llm_daily_limit": req_limit, "llm_used_today": used_req,
            "llm_token_limit": tok_limit, "llm_tokens_today": used_tok}


@app.post("/api/auth/token")
async def auth_set_token(request: Request):
    """Save (or clear, with an empty value) the caller's personal Claude token
    — an OAuth token from `claude setup-token` or an Anthropic API key."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    token = (body.get("token") or "").strip() or None
    user = request.state.user
    request.app.state.users.set_token(user["id"], token)
    return {"has_token": bool(token)}


# --- user management (admin only, enforced by the middleware) ---------------

def _public_user(u):
    # the password IS shown to the admin by request — internal tool. The
    # personal Claude token is the user's own credential: only its presence
    # is exposed, never the value.
    d = {k: u[k] for k in ("id", "username", "password", "role",
                           "created", "last_activity")}
    d["has_token"] = bool(u["api_token"])
    return d


@app.get("/api/users")
async def users_list(request: Request):
    """All accounts: username, password, role, created, last activity."""
    return {"users": [_public_user(u) for u in request.app.state.users.list()]}


@app.post("/api/users")
async def users_create(request: Request):
    """Create an account: {username, password, role: admin|user}."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or "user"
    if not username or not password:
        return JSONResponse({"error": "нужны логин и пароль"}, status_code=422)
    if role not in ROLES:
        return JSONResponse({"error": f"роль: {' | '.join(ROLES)}"}, status_code=422)
    users = request.app.state.users
    if users.get(username):
        return JSONResponse({"error": f"логин «{username}» уже занят"},
                            status_code=409)
    return _public_user(users.create(username, password, role))


@app.put("/api/users/{uid}")
async def users_update(uid: int, request: Request):
    """Edit an account: {username?, password?, role?} — omitted fields keep
    their value. The last admin cannot be demoted."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    users = request.app.state.users
    target = users.get_by_id(uid)
    if target is None:
        return JSONResponse({"error": "пользователь не найден"}, status_code=404)
    username = body.get("username")
    if username is not None:
        username = username.strip()
        if not username:
            return JSONResponse({"error": "логин не может быть пустым"},
                                status_code=422)
        clash = users.get(username)
        if clash and clash["id"] != uid:
            return JSONResponse({"error": f"логин «{username}» уже занят"},
                                status_code=409)
    password = body.get("password")
    if password is not None and not password:
        return JSONResponse({"error": "пароль не может быть пустым"},
                            status_code=422)
    role = body.get("role")
    if role is not None and role not in ROLES:
        return JSONResponse({"error": f"роль: {' | '.join(ROLES)}"}, status_code=422)
    if (role == "user" and target["role"] == "admin"
            and users.admin_count() == 1):
        return JSONResponse({"error": "нельзя понизить последнего администратора"},
                            status_code=400)
    return _public_user(users.update(uid, username=username,
                                     password=password, role=role))


@app.delete("/api/users/{uid}")
async def users_delete(uid: int, request: Request):
    users = request.app.state.users
    target = users.get_by_id(uid)
    if target is None:
        return JSONResponse({"error": "пользователь не найден"}, status_code=404)
    if target["username"] == request.state.user["username"]:
        return JSONResponse({"error": "нельзя удалить самого себя"},
                            status_code=400)
    if target["role"] == "admin" and users.admin_count() == 1:
        return JSONResponse({"error": "нельзя удалить последнего администратора"},
                            status_code=400)
    users.delete(uid)
    return {"ok": True}


@app.get("/api/users/{uid}/history")
async def users_history(uid: int, request: Request, limit: int = 100):
    """One user's analysis runs — the history log on the users screen."""
    users = request.app.state.users
    target = users.get_by_id(uid)
    if target is None:
        return JSONResponse({"error": "пользователь не найден"}, status_code=404)
    return {"requests": request.app.state.web_requests.list_for(
        target["username"], limit)}


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/meta")
async def meta():
    """Static config the web form needs (environment choices, timezone,
    the LLM models offered in the selector and the default one)."""
    return {"environments": WEB_ENVIRONMENTS, "tz_offset_hours": TZ_OFFSET_HOURS,
            "models": WEB_LLM_MODELS, "default_model": ANTHROPIC_MODEL}


# --- runtime settings (admin only, enforced by the middleware) ---------------

def _settings_payload(request: Request):
    s = request.app.state.settings
    return {"system_prompt": s.get("system_prompt") or "",
            "llm_daily_requests": s.get_int("llm_daily_requests", LLM_DAILY_LIMIT),
            "llm_daily_tokens": s.get_int("llm_daily_tokens", LLM_DAILY_TOKENS)}


@app.get("/api/settings")
async def settings_get(request: Request):
    """Runtime settings: the general system prompt (prepended to every web
    analysis) and the daily shared-token limits (0 = off)."""
    return _settings_payload(request)


@app.put("/api/settings")
async def settings_put(request: Request):
    """Edit settings: {system_prompt?, llm_daily_requests?, llm_daily_tokens?}
    — omitted fields keep their value; an empty system_prompt clears it."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    s = request.app.state.settings
    if "system_prompt" in body:
        s.set("system_prompt", (body.get("system_prompt") or "").strip() or None)
    for key in ("llm_daily_requests", "llm_daily_tokens"):
        if key in body:
            # None/empty = unlimited (-1); 0 = shared token forbidden; >0 = cap
            raw = body[key]
            if raw is None or raw == "":
                value = -1
            else:
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    return JSONResponse({"error": f"{key}: нужно число"},
                                        status_code=422)
            s.set(key, max(value, -1))
    return _settings_payload(request)


# --- prepared prompts (reads open to all users; writes admin-only) -----------

@app.get("/api/prompts")
async def prompts_list(request: Request):
    """Prepared prompts for the investigation form: roles (who the answer is
    for) and problem templates (prefill the description)."""
    p = request.app.state.prompts
    return {"roles": p.list("role"), "problems": p.list("problem")}


@app.post("/api/prompts")
async def prompts_create(request: Request):
    """Create a preset: {kind: role|problem, name, text}."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    kind = body.get("kind")
    name = (body.get("name") or "").strip()
    text = (body.get("text") or "").strip()
    if kind not in KINDS:
        return JSONResponse({"error": f"kind: {' | '.join(KINDS)}"},
                            status_code=422)
    if not name or not text:
        return JSONResponse({"error": "нужны название и текст"}, status_code=422)
    return request.app.state.prompts.create(kind, name, text)


@app.put("/api/prompts/{pid}")
async def prompts_update(pid: int, request: Request):
    """Edit a preset: {name?, text?} — omitted field keeps its value."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    name = body.get("name")
    if name is not None and not name.strip():
        return JSONResponse({"error": "название не может быть пустым"},
                            status_code=422)
    text = body.get("text")
    if text is not None and not text.strip():
        return JSONResponse({"error": "текст не может быть пустым"},
                            status_code=422)
    row = request.app.state.prompts.update(
        pid, name=name.strip() if name is not None else None,
        text=text.strip() if text is not None else None)
    if row is None:
        return JSONResponse({"error": "промпт не найден"}, status_code=404)
    return row


@app.delete("/api/prompts/{pid}")
async def prompts_delete(pid: int, request: Request):
    if request.app.state.prompts.get(pid) is None:
        return JSONResponse({"error": "промпт не найден"}, status_code=404)
    request.app.state.prompts.delete(pid)
    return {"ok": True}


@app.post("/api/investigate")
async def investigate_endpoint(request: Request):
    """Investigation form: search Sentry by request_id / device_id / msisdn
    over a period (preset 1h/24h/3d/7d/14d/30d, default 3d, or a custom local
    date range), optionally narrowed to one environment.
    Body: {description, request_id?, device_id?, msisdn?, period?, date_from?,
    date_to?, environment?} — description and at least one identifier are
    required."""
    from app.services.investigation import investigate, ValidationError
    from app.services.sentry_api import discover_error_hint
    sentry = request.app.state.sentry
    if not sentry.enabled:
        return JSONResponse({"error": "Sentry API не настроен (SENTRY_API_TOKEN)."},
                            status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    try:
        result = await investigate(
            sentry,
            description=body.get("description"),
            request_id=body.get("request_id"),
            device_id=body.get("device_id"),
            msisdn=body.get("msisdn"),
            period=body.get("period"),
            date_from=body.get("date_from"),
            date_to=body.get("date_to"),
            environment=body.get("environment"))
    except ValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"error": discover_error_hint(e)}, status_code=502)
    return result


@app.post("/api/explain")
async def explain_endpoint(request: Request):
    """Agentic LLM investigation of a complaint, in plain Russian for support
    staff (the /why analog). Body: same as /api/investigate. The model gets
    ONLY the complaint + identifiers + window and decides itself what to
    search (Sentry + GitLab tools). Slow: up to a few minutes."""
    from app.services.investigation import explain, ValidationError
    sentry_api, gitlab, llm = request.app.state.services
    if not sentry_api.enabled:
        return JSONResponse({"error": "Sentry API не настроен (SENTRY_API_TOKEN)."},
                            status_code=503)
    if not llm.enabled:
        return JSONResponse({"error": "LLM выключен (ENABLE_LLM=false)."},
                            status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    auth_token, own, err = _llm_auth(request)
    if err is not None:
        return err
    model, audience, system_context, err = _explain_params(request, body)
    if err is not None:
        return err
    try:
        rec = await explain(
            sentry_api, gitlab, llm,
            description=body.get("description"),
            request_id=body.get("request_id"),
            device_id=body.get("device_id"),
            msisdn=body.get("msisdn"),
            period=body.get("period"),
            date_from=body.get("date_from"),
            date_to=body.get("date_to"),
            environment=body.get("environment"),
            auth_token=auth_token, model=model, audience=audience,
            system_context=system_context)
    except ValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    uname = request.state.user["username"]
    if rec is None or not rec.text:
        request.app.state.web_requests.add(body, error="LLM call failed",
                                           username=uname, own_token=own)
        return JSONResponse({"error": "Не получилось получить ответ от LLM — "
                                      "смотрите логи sentry-telegram."},
                            status_code=502)
    llm_id = request.app.state.llm_audit.put("web-explain", rec)
    request.app.state.web_requests.add(body, rec=rec, llm_id=llm_id,
                                       username=uname, own_token=own)
    return {"explanation": rec.text, "llm_id": llm_id,
            "in_tokens": rec.in_tokens, "out_tokens": rec.out_tokens,
            "cost": rec.cost}


def _sse(ev):
    return f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/explain/stream")
async def explain_stream(request: Request):
    """Streaming twin of /api/explain (SSE): emits the model's reasoning live
    — status, each thought, each tool call and its result — then the final
    explanation, so the UI can show the run like a chat instead of a spinner.
    Events: {type: status|text|tool|tool_result|done|error, ...}."""
    from app.services.investigation import explain, ValidationError
    sentry_api, gitlab, llm = request.app.state.services
    llm_audit = request.app.state.llm_audit
    if not sentry_api.enabled or not llm.enabled:
        return JSONResponse({"error": "Sentry API или LLM не настроены."},
                            status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    uname = request.state.user["username"]
    auth_token, own, err = _llm_auth(request)
    if err is not None:
        return err
    model, audience, system_context, err = _explain_params(request, body)
    if err is not None:
        return err

    async def gen():
        yield _sse({"type": "status", "message": "запускаю анализ…"})
        queue = asyncio.Queue()
        try:
            task = asyncio.create_task(
                explain(sentry_api, gitlab, llm,
                        description=body.get("description"),
                        request_id=body.get("request_id"),
                        device_id=body.get("device_id"),
                        msisdn=body.get("msisdn"),
                        period=body.get("period"),
                        date_from=body.get("date_from"),
                        date_to=body.get("date_to"),
                        environment=body.get("environment"),
                        on_event=queue.put_nowait,
                        auth_token=auth_token, model=model,
                        audience=audience, system_context=system_context))
        except Exception as e:
            yield _sse({"type": "error", "error": str(e)[:300]})
            return
        try:
            while True:
                get = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({get, task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    yield _sse(get.result())
                    continue
                get.cancel()
                break
            while not queue.empty():           # drain events raced with finish
                yield _sse(queue.get_nowait())
            try:
                rec = task.result()
            except Exception as e:
                request.app.state.web_requests.add(body, error=str(e)[:300],
                                                   username=uname,
                                                   own_token=own)
                yield _sse({"type": "error", "error": str(e)[:300]})
                return
            if rec is None or not rec.text:
                request.app.state.web_requests.add(body, error="LLM call failed",
                                                   username=uname,
                                                   own_token=own)
                yield _sse({"type": "error",
                            "error": "Не получилось получить ответ от LLM — "
                                     "смотрите логи sentry-telegram."})
                return
            llm_id = llm_audit.put("web-explain", rec)
            request.app.state.web_requests.add(body, rec=rec, llm_id=llm_id,
                                               username=uname, own_token=own)
            yield _sse({"type": "done", "explanation": rec.text,
                        "llm_id": llm_id, "in_tokens": rec.in_tokens,
                        "out_tokens": rec.out_tokens, "cost": rec.cost})
        finally:
            task.cancel()                      # client gone -> stop the LLM run

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/history")
async def history_list(request: Request, limit: int = 100):
    """History of web analysis runs: form fields, answer, tokens, llm id."""
    return {"requests": request.app.state.web_requests.list(limit)}


@app.get("/api/projects")
async def projects_list(request: Request):
    """The project catalog: sentry project id -> display name + gitlab repo.
    Unknown projects appear here automatically as events arrive."""
    return {"projects": request.app.state.projects.all()}


@app.put("/api/projects/{pid}")
async def projects_update(pid: str, request: Request):
    """Edit one project: {name?, gitlab_repo?} — omitted field is untouched,
    empty string clears it. gitlab_repo is a full namespace path
    (e.g. mobile/billing) or a numeric GitLab project id."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    row = request.app.state.projects.set(
        pid, name=body.get("name"), gitlab_repo=body.get("gitlab_repo"))
    if row is None:
        return JSONResponse({"error": f"проект {pid} не найден"}, status_code=404)
    return row


@app.get("/api/llm/{llm_id}")
async def llm_call(llm_id: str, request: Request):
    """Full record of one LLM call (prompt, tool trace, tokens, response) by the
    audit id shown under LLM-backed bot replies (llm_a1b2c3; prefix optional)."""
    rec = request.app.state.llm_audit.get(llm_id)
    if rec is None:
        return Response(status_code=404)
    return rec


@app.get("/api/request/{request_id}")
async def request_events(request_id: str, request: Request, period: str = "24h"):
    """All error events of one request id across services (Discover lookup over
    SENTRY_REQUEST_ID_FIELDS). ?period= accepts Sentry statsPeriod (1h/24h/7d)."""
    sentry = request.app.state.sentry
    if not sentry.enabled:
        return Response(status_code=503)
    try:
        key, events = await sentry.events_for_request(request_id, stats_period=period)
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)
    return {"request_id": request_id, "period": period,
            "matched_field": key, "count": len(events), "events": events}


@app.post("/telegram")
async def telegram_webhook(request: Request):
    """
    Telegram update webhook (alternative to polling; set TELEGRAM_POLLING=false).
    Register it once:
      curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-host/telegram"
    """
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)
    await request.app.state.bot.process_update(data)
    return Response(status_code=200)


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    resource = request.headers.get("sentry-hook-resource", "")
    log.info("/webhook called: resource=%s bytes=%d from=%s",
             resource or "?", len(body), request.client.host if request.client else "?")

    # Signature check (Sentry Internal Integration Client Secret). Disabled by default;
    # enable by uncommenting and setting SENTRY_CLIENT_SECRET.
    # from app.sentry.security import verify
    # if not verify(body, request.headers.get("sentry-hook-signature")):
    #     log.warning("/webhook bad signature (resource=%s)", resource or "?")
    #     return Response(status_code=401)

    try:
        payload = await request.json()
    except Exception:
        log.warning("/webhook bad json (resource=%s)", resource or "?")
        return Response(status_code=400)

    # respond right away; do Telegram/LLM work in the background
    asyncio.create_task(request.app.state.pipeline.process(resource, payload))
    return Response(status_code=200)


# The investigation UI normally runs as its own container (sentry-web: nginx
# serving the React build at /admin-web/, forwarding its /admin-web/api/*
# calls to /api/* here). For an all-in-one run without nginx,
# `cd web-ui && npm run build` drops the build into app/web/ and this app
# serves it itself: the UI keeps calling /admin-web/api/*, so those two paths
# are aliased BEFORE the static mount (route order decides — the mount would
# otherwise swallow them).
_WEB_DIR = Path(__file__).resolve().parent / "web"
if _WEB_DIR.is_dir():
    app.add_api_route("/admin-web/api/meta", meta, methods=["GET"])
    app.add_api_route("/admin-web/api/investigate", investigate_endpoint,
                      methods=["POST"])
    app.add_api_route("/admin-web/api/explain", explain_endpoint,
                      methods=["POST"])
    app.add_api_route("/admin-web/api/explain/stream", explain_stream,
                      methods=["POST"])
    app.add_api_route("/admin-web/api/history", history_list, methods=["GET"])
    app.add_api_route("/admin-web/api/projects", projects_list, methods=["GET"])
    app.add_api_route("/admin-web/api/projects/{pid}", projects_update,
                      methods=["PUT"])
    app.add_api_route("/admin-web/api/auth/login", auth_login, methods=["POST"])
    app.add_api_route("/admin-web/api/auth/logout", auth_logout, methods=["POST"])
    app.add_api_route("/admin-web/api/auth/me", auth_me, methods=["GET"])
    app.add_api_route("/admin-web/api/auth/token", auth_set_token,
                      methods=["POST"])
    app.add_api_route("/admin-web/api/settings", settings_get, methods=["GET"])
    app.add_api_route("/admin-web/api/settings", settings_put, methods=["PUT"])
    app.add_api_route("/admin-web/api/prompts", prompts_list, methods=["GET"])
    app.add_api_route("/admin-web/api/prompts", prompts_create,
                      methods=["POST"])
    app.add_api_route("/admin-web/api/prompts/{pid}", prompts_update,
                      methods=["PUT"])
    app.add_api_route("/admin-web/api/prompts/{pid}", prompts_delete,
                      methods=["DELETE"])
    app.add_api_route("/admin-web/api/users", users_list, methods=["GET"])
    app.add_api_route("/admin-web/api/users", users_create, methods=["POST"])
    app.add_api_route("/admin-web/api/users/{uid}", users_update, methods=["PUT"])
    app.add_api_route("/admin-web/api/users/{uid}", users_delete,
                      methods=["DELETE"])
    app.add_api_route("/admin-web/api/users/{uid}/history", users_history,
                      methods=["GET"])
    app.mount("/admin-web", StaticFiles(directory=_WEB_DIR, html=True), name="web")

    @app.get("/")
    async def index():
        return RedirectResponse("/admin-web/")
else:
    log.info("web UI not mounted (app/web absent) — it runs as the sentry-web container")
