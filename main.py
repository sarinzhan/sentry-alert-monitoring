"""
Entry point: Sentry -> Telegram notifier.

Run on a server:
  pip install -r requirements.txt
  cp .env.example .env   &&   edit .env
  python main.py
"""
import uvicorn

from config import BOT_TOKEN, HOST, PORT
from controller import app


if __name__ == "__main__":
    # TELEGRAM_CHAT_ID is optional now: alerts are opt-in per chat (/subscribe).
    # If it's set, it's seeded once as "subscribed to all"; if it's deleted, chats
    # simply self-subscribe. Only the bot token is truly required.
    if not BOT_TOKEN:
        raise SystemExit("set TELEGRAM_BOT_TOKEN (in .env or env)")
    uvicorn.run(app, host=HOST, port=PORT)
