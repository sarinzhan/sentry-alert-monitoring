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

# Project id -> display NAME for the message header, e.g. "3:s_billing,4:billing".
# Checked before the Sentry API lookup. Accept SENTRY_PROJECTS (preferred) or the
# older PROJECT_NAMES. This is separate from GITLAB_PROJECTS (id -> repo path) below.
PROJECT_NAMES = {}
for _pair in (os.environ.get("SENTRY_PROJECTS") or os.environ.get("PROJECT_NAMES", "")).split(","):
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
GITLAB_REF   = os.environ.get("GITLAB_REF") or os.environ.get("GITLAB_DEFAULT_REF", "main")
GITLAB_CONTEXT_LINES = int(os.environ.get("GITLAB_CONTEXT_LINES", "25"))
# Debug: build and log the LLM prompt for EVERY event, including debounced ones
# (normally the prompt is only built for events we actually send). Verbose + makes
# GitLab calls per event — turn off after debugging.
LOG_LLM_PROMPT = os.environ.get("LOG_LLM_PROMPT", "false").lower() == "true"
# accept either name (GITLAB_PROJECTS or the older PROJECT_REPOS)
GITLAB_PROJECTS = {}
for _pair in (os.environ.get("GITLAB_PROJECTS") or os.environ.get("PROJECT_REPOS", "")).split(","):
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

# --- alert-trigger model ---
# Send an alert for an issue when ANY of:
#   new         first time we see the issue
#   ongoing     >= WINDOW_INTERVAL_FROM_LAST_ALERT_IN_HOUR since the last alert (min gap)
#   escalating  within WINDOW_CRITICAL_INTERVAL_IN_MINUTE: occurrences > CRITICAL_ERROR_THRESHOLD
#               OR distinct affected users >= AFFECTED_USER_THRESHOLD; at most once per
#               WINDOW_INTERVAL_FOR_CRITICAL_IN_HOUR.
WINDOW_INTERVAL_FROM_LAST_ALERT_IN_HOUR = float(os.environ.get("WINDOW_INTERVAL_FROM_LAST_ALERT_IN_HOUR", "12"))
WINDOW_CRITICAL_INTERVAL_IN_MINUTE      = float(os.environ.get("WINDOW_CRITICAL_INTERVAL_IN_MINUTE", "10"))
CRITICAL_ERROR_THRESHOLD                = int(os.environ.get("CRITICAL_ERROR_THRESHOLD", "15"))   # occurrences, strict >
AFFECTED_USER_THRESHOLD                 = int(os.environ.get("AFFECTED_USER_THRESHOLD", "5"))     # distinct users, >=
WINDOW_INTERVAL_FOR_CRITICAL_IN_HOUR    = float(os.environ.get("WINDOW_INTERVAL_FOR_CRITICAL_IN_HOUR", "4"))

ONGOING_INTERVAL_SEC   = int(WINDOW_INTERVAL_FROM_LAST_ALERT_IN_HOUR * 3600)
CRITICAL_WINDOW_SEC    = int(WINDOW_CRITICAL_INTERVAL_IN_MINUTE * 60)
CRITICAL_RATELIMIT_SEC = int(WINDOW_INTERVAL_FOR_CRITICAL_IN_HOUR * 3600)

# Mute limits (days) for /mute and /mute_project.
MUTE_MAX_DAYS         = int(os.environ.get("MUTE_MAX_DAYS", "7"))
PROJECT_MUTE_MAX_DAYS = int(os.environ.get("PROJECT_MUTE_MAX_DAYS", "15"))
# Keyword force-send: matching alerts are sent bypassing debounce+mute. 0 = every event
# ("во всех случаях"); set >0 seconds as an anti-spam floor between forced sends per issue.
KEYWORD_MIN_INTERVAL_SEC = int(os.environ.get("KEYWORD_MIN_INTERVAL_SEC", "0"))

# The three occurrence-count windows shown on line 2 of the message (e.g. 2343/43/22),
# specified in MINUTES. Default "720,360,10" = 12h / 6h / 10m. Stored as seconds.
STAT_WINDOWS = [int(x) * 60 for x in os.environ.get("STAT_WINDOWS", "720,360,10").split(",")
                if x.strip().isdigit()]
if len(STAT_WINDOWS) != 3:
    STAT_WINDOWS = [720 * 60, 360 * 60, 10 * 60]


def _wlabel(sec):
    """Seconds -> compact h/m label for the message and /params."""
    for unit, n in (("h", 3600), ("m", 60)):
        if sec % n == 0:
            return f"{sec // n}{unit}"
    return f"{sec}s"

# Future feature: ask an LLM for likely cause + fix. Off by default.
ENABLE_LLM         = os.environ.get("ENABLE_LLM", "false").lower() == "true"
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL    = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
# Hard ceiling on the LLM's *output* length (a cap, not a target — billed per token
# actually generated). Too low truncates the cause/fix mid-sentence.
ANTHROPIC_MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "1024"))
# Price per 1M tokens, for the cost line in the message. Defaults = Opus 4.8 ($5/$25).
ANTHROPIC_PRICE_IN  = float(os.environ.get("ANTHROPIC_PRICE_IN", "5.0"))
ANTHROPIC_PRICE_OUT = float(os.environ.get("ANTHROPIC_PRICE_OUT", "25.0"))
# Stack sent to the LLM: keep the full in-app (project) trace, but at most this many
# library frames (framework noise). All-lib crashes still show the top few for context.
LLM_STACK_LIB_MAX = int(os.environ.get("LLM_STACK_LIB_MAX", "5"))
# TLS for the external Anthropic call. api.anthropic.com goes through the corporate
# proxy, which MITMs the cert — same problem as Telegram. Defaults to the Telegram
# posture; point ANTHROPIC_CA_BUNDLE at the corporate CA to verify instead of skip.
ANTHROPIC_SSL_INSECURE = os.environ.get(
    "ANTHROPIC_SSL_INSECURE", str(TELEGRAM_SSL_INSECURE)).lower() == "true"
ANTHROPIC_CA_BUNDLE = os.environ.get("ANTHROPIC_CA_BUNDLE", "").strip()
if ANTHROPIC_CA_BUNDLE == "-":
    ANTHROPIC_CA_BUNDLE = ""


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sentry-telegram")


def _mask(v):
    """Show only the ends of a secret so the log is safe but still verifiable."""
    if not v:
        return "-"
    s = str(v)
    return "****" if len(s) <= 8 else f"{s[:4]}…{s[-4:]} (len {len(s)})"


def banner():
    """Multi-line summary of the effective config at startup (secrets masked)."""
    chat = f"{CHAT_ID}" + (f" topic={CHAT_THREAD_ID}" if CHAT_THREAD_ID else "")
    lines = [
        "=" * 64,
        " sentry-telegram — starting",
        "=" * 64,
        f"  listen            {HOST}:{PORT}",
        f"  db                {DB_PATH}",
        f"  telegram bot      {_mask(BOT_TOKEN)}",
        f"  telegram chat     {chat}",
        f"  telegram polling  {TELEGRAM_POLLING}",
        f"  telegram tls      insecure={TELEGRAM_SSL_INSECURE} ca={TELEGRAM_CA_BUNDLE or '-'}",
        f"  sentry signature  {'on (' + _mask(CLIENT_SECRET) + ')' if CLIENT_SECRET else 'OFF — no verification'}",
        f"  sentry api        url={SENTRY_API_URL} org={SENTRY_ORG} token={_mask(SENTRY_API_TOKEN)}",
        f"  project names     {PROJECT_NAMES or '-'}",
        f"  triggers          ongoing>={WINDOW_INTERVAL_FROM_LAST_ALERT_IN_HOUR}h · "
        f"critical: >{CRITICAL_ERROR_THRESHOLD} err OR >={AFFECTED_USER_THRESHOLD} usr "
        f"in {WINDOW_CRITICAL_INTERVAL_IN_MINUTE}m, max 1/{WINDOW_INTERVAL_FOR_CRITICAL_IN_HOUR}h",
        f"  mute limits       issue<={MUTE_MAX_DAYS}d project<={PROJECT_MUTE_MAX_DAYS}d "
        f"kw_min_interval={KEYWORD_MIN_INTERVAL_SEC}s",
        f"  stat windows      {'/'.join(_wlabel(w) for w in STAT_WINDOWS)}",
        f"  llm               enabled={ENABLE_LLM} model={ANTHROPIC_MODEL} "
        f"max_tokens={ANTHROPIC_MAX_TOKENS} key={_mask(ANTHROPIC_API_KEY)}",
        f"  llm tls           insecure={ANTHROPIC_SSL_INSECURE} ca={ANTHROPIC_CA_BUNDLE or '-'}",
        f"  llm stack         lib_max={LLM_STACK_LIB_MAX}",
        f"  gitlab            url={GITLAB_URL or '-'} ref={GITLAB_REF} "
        f"token={_mask(GITLAB_TOKEN)} ctx_lines={GITLAB_CONTEXT_LINES}",
        f"  gitlab projects   {GITLAB_PROJECTS or '-'}",
        f"  debug             log_raw_payload={LOG_RAW_PAYLOAD} log_llm_prompt={LOG_LLM_PROMPT}",
        "=" * 64,
    ]
    return "\n".join(lines)


def params_summary():
    """Operational parameters (no secrets) for the /params chat command."""
    return "\n".join([
        "ongoing (min gap):        %gh" % WINDOW_INTERVAL_FROM_LAST_ALERT_IN_HOUR,
        "critical window:          %gm" % WINDOW_CRITICAL_INTERVAL_IN_MINUTE,
        "critical error threshold: >%d" % CRITICAL_ERROR_THRESHOLD,
        "affected user threshold:  >=%d" % AFFECTED_USER_THRESHOLD,
        "critical rate limit:      1 / %gh" % WINDOW_INTERVAL_FOR_CRITICAL_IN_HOUR,
        "stat windows (line 2):    %s" % "/".join(_wlabel(w) for w in STAT_WINDOWS),
        "mute max:                 issue %dd · project %dd" % (MUTE_MAX_DAYS, PROJECT_MUTE_MAX_DAYS),
        "keyword min interval:     %ds" % KEYWORD_MIN_INTERVAL_SEC,
        "llm:                      enabled=%s model=%s max_tokens=%d" % (
            ENABLE_LLM, ANTHROPIC_MODEL, ANTHROPIC_MAX_TOKENS),
        "gitlab source:            %s" % ("on" if (GITLAB_URL and GITLAB_TOKEN and GITLAB_PROJECTS) else "off"),
    ])
