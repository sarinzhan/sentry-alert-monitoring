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
    # Alerts are opt-in per chat (/subscribe) — there is no global chat. Only the
    # bot token is required.
    if not BOT_TOKEN:
        raise SystemExit("set TELEGRAM_BOT_TOKEN (in .env or env)")
    uvicorn.run(app, host=HOST, port=PORT)
