"""Configuration: tiny .env loader, settings, and shared logging."""
import os
import logging


def _load_dotenv(path=".env"):
    """Tiny .env loader so we don't need an extra dependency."""
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


# --- core ---
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID")
CLIENT_SECRET = os.environ.get("SENTRY_CLIENT_SECRET")        # empty -> signature check off
DB_PATH       = os.environ.get("DB_PATH", "state.db")
HOST          = os.environ.get("HOST", "0.0.0.0")
PORT          = int(os.environ.get("PORT", "8080"))

# TLS for the Telegram API. On corporate networks the bot reaches api.telegram.org
# through an intercepting HTTPS proxy whose internal CA must be trusted. Point this
# at the corporate CA bundle (PEM). Leave empty to use the system/certifi trust store.
TELEGRAM_CA_BUNDLE = os.environ.get("TELEGRAM_CA_BUNDLE", "")
# Last resort: skip certificate verification entirely (insecure). Only meaningful when
# you're already behind a trusted MITM proxy and can't obtain a clean CA cert.
TELEGRAM_SSL_INSECURE = os.environ.get("TELEGRAM_SSL_INSECURE", "false").lower() == "true"

# Long-poll Telegram for incoming commands (/start) instead of needing a public
# webhook. Handy for local use. Leave off in production if you use setWebhook,
# since getUpdates and a webhook can't both be active (Telegram returns 409).
TELEGRAM_POLLING = os.environ.get("TELEGRAM_POLLING", "true").lower() == "true"

# Debounce. The first occurrence of an issue sends immediately; these are the
# required gaps (seconds) before the 2nd, 3rd, ... send for the SAME issue.
# The last value repeats forever. [60, 300] = "now, then >=1m, then every 5m".
SEND_WINDOWS = [60, 300]

# Future feature: ask an LLM for likely cause + fix. Off by default.
ENABLE_LLM        = os.environ.get("ENABLE_LLM", "false").lower() == "true"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sentry-telegram")
