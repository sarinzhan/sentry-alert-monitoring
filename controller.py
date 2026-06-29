"""
Controller — the FastAPI app: HTTP endpoints and the app lifespan that wires the
ChatBotHandler and SentryEventHandler together.

Endpoints:
  GET  /health    liveness check
  POST /webhook   Sentry webhook  -> SentryEventHandler
  POST /telegram  Telegram webhook -> ChatBotHandler (alternative to polling)
"""
import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

from config import BOT_TOKEN, DB_PATH, SEND_WINDOWS, ENABLE_LLM, TELEGRAM_POLLING, log
from chat_bot_handler import ChatBotHandler
from sentry_event_handler import SentryEventHandler


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = httpx.AsyncClient(timeout=15)
    bot = ChatBotHandler(BOT_TOKEN)
    sentry = SentryEventHandler(send=bot.send, client=client)

    app.state.client = client
    app.state.bot = bot
    app.state.sentry = sentry

    await bot.start(polling=TELEGRAM_POLLING)
    log.info("started; db=%s windows=%s llm=%s polling=%s",
             DB_PATH, SEND_WINDOWS, ENABLE_LLM, TELEGRAM_POLLING)
    yield

    await bot.stop()
    await client.aclose()


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


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    resource = request.headers.get("sentry-hook-resource", "")
    log.info("/webhook called: resource=%s bytes=%d from=%s",
             resource or "?", len(body), request.client.host if request.client else "?")

    sentry = request.app.state.sentry
    if not sentry.verify(body, request.headers.get("sentry-hook-signature")):
        log.warning("/webhook bad signature (resource=%s)", resource or "?")
        return Response(status_code=401)

    try:
        payload = await request.json()
    except Exception:
        log.warning("/webhook bad json (resource=%s)", resource or "?")
        return Response(status_code=400)

    # respond right away; do Telegram/LLM work in the background
    asyncio.create_task(sentry.process(resource, payload))
    return Response(status_code=200)
