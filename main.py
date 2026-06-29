"""
Entry point: Sentry -> Telegram notifier.

Run on a server:
  pip install -r requirements.txt
  cp .env.example .env   &&   edit .env
  python main.py
"""
import uvicorn

from config import BOT_TOKEN, CHAT_ID, HOST, PORT
from controller import app


if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (in .env or env)")
    uvicorn.run(app, host=HOST, port=PORT)
