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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import BOT_TOKEN, TELEGRAM_POLLING, TZ_OFFSET_HOURS, WEB_ENVIRONMENTS, log
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

    # --- external services ---
    sentry_api = SentryApiClient()
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

@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/meta")
async def meta():
    """Static config the web form needs (environment choices, timezone)."""
    return {"environments": WEB_ENVIRONMENTS, "tz_offset_hours": TZ_OFFSET_HOURS}


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
    app.mount("/admin-web", StaticFiles(directory=_WEB_DIR, html=True), name="web")

    @app.get("/")
    async def index():
        return RedirectResponse("/admin-web/")
else:
    log.info("web UI not mounted (app/web absent) — it runs as the sentry-web container")
