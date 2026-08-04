"""Controller — the FastAPI app: HTTP endpoints and the composition root.

The lifespan builds the whole object graph (Database → repositories → services →
pipeline → bot), injects the command dependencies, and starts the bot.

Endpoints:
  GET  /health    liveness check
  POST /webhook   Sentry webhook   -> EventPipeline
  POST /telegram  Telegram webhook -> ChatBotHandler (alternative to polling)
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app.config import BOT_TOKEN, TELEGRAM_POLLING, log
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
from app.telegram.commands import register_all


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
    key, events = await sentry.events_for_request(request_id, stats_period=period)
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
