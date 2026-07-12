"""Controller — the FastAPI app: HTTP endpoints and the composition root.

The lifespan builds the whole object graph (Database → repositories → services →
pipeline → bot), injects the command dependencies, and starts the bot.

Endpoints:
  GET  /health    liveness check
  POST /webhook   Sentry webhook   -> EventPipeline
  POST /telegram  Telegram webhook -> ChatBotHandler (alternative to polling)
  POST /ask       question -> AgentService (x-api-key protected; off if no key)
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import BOT_TOKEN, TELEGRAM_POLLING, ASK_API_KEY, log
from app.summaries import banner
from app.db import Database
from app.repositories.issues import IssuesRepo
from app.repositories.subscriptions import SubscriptionsRepo
from app.repositories.rules import RulesRepo
from app.repositories.chat_state import ChatStateRepo
from app.repositories.keywords import KeywordsRepo
from app.repositories.usermap import UserMapRepo
from app.repositories.context import ContextRepo
from app.services.sentry_api import SentryApiClient
from app.services.sentry_query import SentryQueryClient
from app.services.gitlab import GitLabClient
from app.sentry.decision import Decider
from app.sentry.analysis import AnalysisService
from app.sentry.agent import AgentService
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

    # --- external services ---
    sentry_api = SentryApiClient()
    sentry_query = SentryQueryClient()
    gitlab = GitLabClient()

    # --- domain ---
    # Claude Agent SDK agent: Sentry + GitLab tools. Serves /ask, /ai and the
    # webhook-path analysis. Closes the SentryQueryClient it wraps.
    agent = AgentService(sentry_query, gitlab)
    analysis = AnalysisService(issues, context, gitlab, agent)
    decider = Decider(chat_state, issues)

    # --- telegram + pipeline (bot.send is the pipeline's sender) ---
    bot = ChatBotHandler(BOT_TOKEN)
    pipeline = EventPipeline(
        issues=issues, subscriptions=subscriptions, rules=rules, keywords=keywords,
        usermap=usermap, context=context, decider=decider, sentry_api=sentry_api,
        gitlab=gitlab, analysis=analysis, sender=bot.send)

    deps = Deps(issues=issues, subscriptions=subscriptions, rules=rules,
                keywords=keywords, usermap=usermap, analysis=analysis,
                pipeline=pipeline, agent=agent, context=context)
    register_all(bot.app, deps)

    app.state.bot = bot
    app.state.pipeline = pipeline
    app.state.agent = agent
    app.state.db = db
    # agent.aclose() also closes sentry_query, so it's not listed separately.
    app.state.services = (sentry_api, gitlab, agent)

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


@app.post("/ask")
async def ask_endpoint(request: Request):
    """Ask the monitoring agent a question (for web UIs / other services).

    Request:  {"question": "why does GET /api/orders return 500?"}
    Header:   x-api-key: <ASK_API_KEY>
    Response: {"answer": ..., "cost_usd": ..., "in_tokens": ..., "out_tokens": ...}
    Fail-closed: 401 unless ASK_API_KEY is configured AND matches.
    """
    if not ASK_API_KEY or request.headers.get("x-api-key") != ASK_API_KEY:
        return Response(status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    question = str(body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)
    agent = request.app.state.agent
    if not agent.enabled:
        return JSONResponse({"error": "llm disabled"}, status_code=503)
    ans = await agent.ask(question)
    if ans is None:
        return JSONResponse({"error": "agent failed"}, status_code=502)
    return {"answer": ans.text, "cost_usd": round(ans.cost, 6),
            "in_tokens": ans.in_tokens, "out_tokens": ans.out_tokens}


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
