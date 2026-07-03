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
# TELEGRAM_CHAT_ID may be a bare chat id ("-100123...") or "chatid:thread" to post
# into a specific forum topic ("-100123...:8"). Split it into chat + default thread.
_raw_chat     = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
CHAT_ID       = _raw_chat or None
CHAT_THREAD_ID = None
if _raw_chat and ":" in _raw_chat:
    _cid, _, _tid = _raw_chat.partition(":")
    CHAT_ID = _cid.strip() or None
    _tid = _tid.strip()
    CHAT_THREAD_ID = int(_tid) if _tid.lstrip("-").isdigit() else None
CLIENT_SECRET = (os.environ.get("SENTRY_CLIENT_SECRET") or "").strip() or None  # empty -> signature check off
DB_PATH       = os.environ.get("DB_PATH", "state.db")
HOST          = os.environ.get("HOST", "0.0.0.0")
PORT          = int(os.environ.get("PORT", "8080"))

# Sentry API — used to resolve a project's numeric id to its name (error webhooks
# only carry the id). Reach Sentry internally on the shared docker network so the
# call bypasses the corporate proxy. Set SENTRY_API_TOKEN to enable the lookup.
SENTRY_API_URL   = os.environ.get("SENTRY_API_URL", "http://web:9000").rstrip("/")
SENTRY_ORG       = os.environ.get("SENTRY_ORG", "sentry")
SENTRY_API_TOKEN = os.environ.get("SENTRY_API_TOKEN", "").strip()
if SENTRY_API_TOKEN == "-":            # placeholder for "not set"
    SENTRY_API_TOKEN = ""

# Optional static project id -> name map for the header, e.g. "3:billing,4:payments".
# Checked before the API lookup; the reliable option when you can't use an API token.
PROJECT_NAMES = {}
for _pair in os.environ.get("PROJECT_NAMES", "").split(","):
    if ":" in _pair:
        _k, _v = _pair.split(":", 1)
        if _k.strip() and _v.strip():
            PROJECT_NAMES[_k.strip()] = _v.strip()

# GitLab source lookup — pull the failing file so the LLM sees real code, not just
# a stack. GITLAB_PROJECTS maps a Sentry project id to a GitLab project (path or
# numeric id), e.g. "3:mobile/billing-service,4:mobile/payments". Ref defaults to
# GITLAB_REF (releases aren't tied to commits yet, so we read the branch head).
GITLAB_URL   = os.environ.get("GITLAB_URL", "").rstrip("/")
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "").strip()
GITLAB_REF   = os.environ.get("GITLAB_REF", "main")
GITLAB_CONTEXT_LINES = int(os.environ.get("GITLAB_CONTEXT_LINES", "25"))
GITLAB_PROJECTS = {}
for _pair in os.environ.get("GITLAB_PROJECTS", "").split(","):
    if ":" in _pair:
        _k, _v = _pair.split(":", 1)
        if _k.strip() and _v.strip():
            GITLAB_PROJECTS[_k.strip()] = _v.strip()

# TLS for the Telegram API. On corporate networks the bot reaches api.telegram.org
# through an intercepting HTTPS proxy whose internal CA must be trusted. Point this
# at the corporate CA bundle (PEM). Leave empty to use the system/certifi trust store.
TELEGRAM_CA_BUNDLE = os.environ.get("TELEGRAM_CA_BUNDLE", "").strip()
if TELEGRAM_CA_BUNDLE == "-":          # placeholder for "not set"
    TELEGRAM_CA_BUNDLE = ""
# Last resort: skip certificate verification entirely (insecure). Only meaningful when
# you're already behind a trusted MITM proxy and can't obtain a clean CA cert.
TELEGRAM_SSL_INSECURE = os.environ.get("TELEGRAM_SSL_INSECURE", "false").lower() == "true"

# Long-poll Telegram for incoming commands (/start) instead of needing a public
# webhook. Handy for local use. Leave off in production if you use setWebhook,
# since getUpdates and a webhook can't both be active (Telegram returns 409).
TELEGRAM_POLLING = os.environ.get("TELEGRAM_POLLING", "true").lower() == "true"

# Debug: dump the raw Sentry webhook payload to the log (verbose). Handy for
# inspecting payload shape (e.g. what the `project` field actually contains).
LOG_RAW_PAYLOAD = os.environ.get("LOG_RAW_PAYLOAD", "true").lower() == "true"

# Debounce. The first occurrence of an issue sends immediately; these are the
# required gaps (seconds) before the 2nd, 3rd, ... send for the SAME issue.
# The last value repeats forever. [60, 300] = "now, then >=1m, then every 5m".
SEND_WINDOWS = [60, 300]

# Escalation rule: if an issue fires >= SPIKE_THRESHOLD times SINCE the last message
# we sent for it, send an "escalating" alert even if the debounce would suppress it.
# Sending resets that baseline, so it re-fires only after another THRESHOLD events.
# 0 disables escalation.
SPIKE_THRESHOLD = int(os.environ.get("SPIKE_THRESHOLD", "5"))

# Future feature: ask an LLM for likely cause + fix. Off by default.
ENABLE_LLM         = os.environ.get("ENABLE_LLM", "false").lower() == "true"
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL    = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
# Hard ceiling on the LLM's *output* length (a cap, not a target — billed per token
# actually generated). Too low truncates the cause/fix mid-sentence.
ANTHROPIC_MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "1024"))


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sentry-telegram")
