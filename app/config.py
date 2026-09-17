"""Configuration: tiny .env loader, settings, and shared logging.

Pure settings only — no formatting/rendering. The human-facing summaries
(banner, /params) live in app.summaries.
"""
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
# Alerts are fully opt-in per chat: a chat subscribes to projects with /subscribe.
# There is no default/global chat — the bot only posts where a chat has subscribed.
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN")
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

# --- Discover lookups (/req, /why, /activity) ---
# Comma-separated Sentry search keys, tried in order until one returns hits.
# Where the user's msisdn lives in events: user.id / user.username / a tag name.
SENTRY_MSISDN_FIELDS = [f.strip() for f in os.environ.get(
    "SENTRY_MSISDN_FIELDS", "user.id,user.username,msisdn").split(",") if f.strip()]
# Where the request id lives: a tag name, or 'trace' for the distributed trace id.
SENTRY_REQUEST_ID_FIELDS = [f.strip() for f in os.environ.get(
    "SENTRY_REQUEST_ID_FIELDS", "request_id,trace").split(",") if f.strip()]
# Where the device id lives (web investigation form).
SENTRY_DEVICE_ID_FIELDS = [f.strip() for f in os.environ.get(
    "SENTRY_DEVICE_ID_FIELDS", "deviceId,device_id").split(",") if f.strip()]
# Environment choices offered by the web form (must match Sentry environment
# names). The form also offers "all environments" regardless of this list.
WEB_ENVIRONMENTS = [e.strip() for e in os.environ.get(
    "WEB_ENVIRONMENTS", "prod,stage").split(",") if e.strip()]
# Times users type in commands (/why) are local; Sentry stores UTC. Default +6 (Bishkek).
TZ_OFFSET_HOURS = float(os.environ.get("TIMEZONE_OFFSET_HOURS", "6"))
# Sentry Logs dataset: services ship application log lines to Sentry, so user
# ACTIONS (not only errors) are searchable. Empty disables the log lookups.
SENTRY_LOGS_DATASET = os.environ.get("SENTRY_LOGS_DATASET", "logs").strip()
# How to find one user's log lines. Some services set the msisdn ATTRIBUTE
# (v4-service), others only mention it inside the message text (configurator),
# hence attribute OR full-text by default.
SENTRY_LOGS_MSISDN_QUERY = os.environ.get(
    "SENTRY_LOGS_MSISDN_QUERY", '(msisdn:"{value}" OR message:"{value}")')
# Which projects Discover queries cover. Default -1 = ALL projects; without an
# explicit value Sentry falls back to the token's "member projects" only.
# Comma-separated numeric ids to narrow (e.g. "2,3,6").
SENTRY_SEARCH_PROJECTS = [p.strip() for p in os.environ.get(
    "SENTRY_SEARCH_PROJECTS", "-1").split(",") if p.strip()]
# Environments to search; empty = all (prod, stage, dev, ...).
SENTRY_ENVIRONMENTS = [e.strip() for e in os.environ.get(
    "SENTRY_ENVIRONMENTS", "").split(",") if e.strip()]

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

# Long-poll Telegram for incoming commands instead of needing a public webhook.
# Handy for local use. Leave off in production if you use setWebhook, since
# getUpdates and a webhook can't both be active (Telegram returns 409).
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

# Per-project window: at most one message per (chat, project) within this window,
# regardless of how many distinct issues fire or their status (escalating too).
# Only keyword force-send bypasses it. 0 = off. Suppressed alerts are deferred,
# not lost: the next event after the window sends as usual.
WINDOW_PROJECT_IN_MINUTE = float(os.environ.get("WINDOW_PROJECT_IN_MINUTE", "10"))
PROJECT_WINDOW_SEC       = int(WINDOW_PROJECT_IN_MINUTE * 60)

# Keyword force-send: matching alerts are sent bypassing the min gap. 0 = every event
# ("во всех случаях"); set >0 seconds as an anti-spam floor between forced sends per issue.
KEYWORD_MIN_INTERVAL_SEC = int(os.environ.get("KEYWORD_MIN_INTERVAL_SEC", "0"))

# The three occurrence-count windows shown on line 2 of the message (e.g. 2343/43/22),
# specified in MINUTES. Default "720,360,10" = 12h / 6h / 10m. Stored as seconds.
STAT_WINDOWS = [int(x) * 60 for x in os.environ.get("STAT_WINDOWS", "720,360,10").split(",")
                if x.strip().isdigit()]
if len(STAT_WINDOWS) != 3:
    STAT_WINDOWS = [720 * 60, 360 * 60, 10 * 60]

# --- LLM (Claude Agent SDK) ---
ENABLE_LLM         = os.environ.get("ENABLE_LLM", "false").lower() == "true"
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")
# Alternative auth: Claude subscription OAuth token (Pro/Max) from `claude setup-token`,
# valid ~1 year. If both are set the API key wins (matches the SDK's precedence).
CLAUDE_CODE_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip() or None
LLM_AUTH_OK        = bool(ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN)
ANTHROPIC_MODEL    = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
# Each LLM call spawns the SDK's `claude` subprocess — cap how many run at once.
AGENT_MAX_CONCURRENCY = int(os.environ.get("AGENT_MAX_CONCURRENCY", "2"))
# Agentic analysis (/ai): give the LLM read-only GitLab tools so it can dig
# through the repo itself (read files, grep, blame, recent commits). Applies to
# the on-demand /ai command only — pipeline alerts keep the cheaper one-shot.
ENABLE_LLM_TOOLS = os.environ.get("ENABLE_LLM_TOOLS", "true").lower() == "true"
# Turn ceiling for the tool loop (each turn = one model call; tool results come
# back between turns). Only used when tools are attached; one-shot stays at 1.
AGENT_MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "15"))
# Hard ceiling on the LLM's *output* length (a cap, not a target — billed per token
# actually generated). Too low truncates the cause/fix mid-sentence.
ANTHROPIC_MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "1024"))
# USD -> Kyrgyz som (KGS) rate, to also show the cost in сом. Update as needed.
# (With subscription OAuth the SDK reports no dollar cost — the message shows
# only the token counts, so the rate matters for api-key auth only.)
USD_KGS_RATE = float(os.environ.get("USD_KGS_RATE", "89.5"))
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


# The global defaults every chat inherits until it overrides a value with /set.
# Per-chat overrides live in the DB (see repositories.rules.RulesRepo).
DEFAULT_RULES = {
    "ongoing_sec":             ONGOING_INTERVAL_SEC,
    "critical_window_sec":     CRITICAL_WINDOW_SEC,
    "critical_threshold":      CRITICAL_ERROR_THRESHOLD,
    "affected_user_threshold": AFFECTED_USER_THRESHOLD,
    "critical_ratelimit_sec":  CRITICAL_RATELIMIT_SEC,
    "project_window_sec":      PROJECT_WINDOW_SEC,
    "stat_windows":            list(STAT_WINDOWS),
    "statuses":                None,     # None = all (new/ongoing/escalating)
}

# The three alert statuses a chat can opt in/out of.
ALERT_STATUSES = ("new", "ongoing", "escalating")


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sentry-telegram")
