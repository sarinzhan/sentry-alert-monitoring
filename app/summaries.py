"""Human-facing config rendering: duration parsing, startup banner, /params text.

Kept separate from app.config so settings stay pure data.
"""
from app import config as c


def fmt_duration(sec):
    """Seconds -> compact d/h/m/s label (e.g. 43200 -> '12h')."""
    sec = int(sec)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec and sec % n == 0:
            return f"{sec // n}{unit}"
    return f"{sec}s"


def parse_duration(s):
    """'12h' / '10m' / '30s' / '1d' / bare-seconds -> int seconds, or None if unparseable."""
    s = str(s).strip().lower()
    if not s:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    try:
        if s[-1] in units:
            return int(float(s[:-1]) * units[s[-1]])
        return int(float(s))
    except (ValueError, IndexError):
        return None


def _mask(v):
    """Show only the ends of a secret so the log is safe but still verifiable."""
    if not v:
        return "-"
    s = str(v)
    return "****" if len(s) <= 8 else f"{s[:4]}…{s[-4:]} (len {len(s)})"


def banner():
    """Multi-line summary of the effective config at startup (secrets masked)."""
    lines = [
        "=" * 64,
        " sentry-telegram — starting",
        "=" * 64,
        f"  listen            {c.HOST}:{c.PORT}",
        f"  db                {c.DB_PATH}",
        f"  telegram bot      {_mask(c.BOT_TOKEN)}",
        f"  telegram chat     opt-in: chats self-subscribe with /subscribe",
        f"  telegram polling  {c.TELEGRAM_POLLING}",
        f"  telegram tls      insecure={c.TELEGRAM_SSL_INSECURE} ca={c.TELEGRAM_CA_BUNDLE or '-'}",
        f"  sentry signature  {'on (' + _mask(c.CLIENT_SECRET) + ')' if c.CLIENT_SECRET else 'OFF — no verification'}",
        f"  sentry api        url={c.SENTRY_API_URL} org={c.SENTRY_ORG} token={_mask(c.SENTRY_API_TOKEN)}",
        f"  project names     {c.PROJECT_NAMES or '-'}",
        f"  triggers          ongoing>={c.WINDOW_INTERVAL_FROM_LAST_ALERT_IN_HOUR}h · "
        f"critical: >{c.CRITICAL_ERROR_THRESHOLD} err OR >={c.AFFECTED_USER_THRESHOLD} usr "
        f"in {c.WINDOW_CRITICAL_INTERVAL_IN_MINUTE}m, max 1/{c.WINDOW_INTERVAL_FOR_CRITICAL_IN_HOUR}h",
        f"  keyword           kw_min_interval={c.KEYWORD_MIN_INTERVAL_SEC}s",
        f"  stat windows      {'/'.join(fmt_duration(w) for w in c.STAT_WINDOWS)}",
        f"  llm               enabled={c.ENABLE_LLM} model={c.ANTHROPIC_MODEL} "
        f"auth={'api-key ' + _mask(c.ANTHROPIC_API_KEY) if c.ANTHROPIC_API_KEY else ('oauth ' + _mask(c.CLAUDE_CODE_OAUTH_TOKEN) if c.CLAUDE_CODE_OAUTH_TOKEN else '-')}",
        f"  llm tls           insecure={c.ANTHROPIC_SSL_INSECURE} ca={c.ANTHROPIC_CA_BUNDLE or '-'}",
        f"  llm stack         lib_max={c.LLM_STACK_LIB_MAX}",
        f"  agent             enabled={bool(c.ENABLE_LLM and c.LLM_AUTH_OK)} "
        f"turns={c.AGENT_MAX_TURNS} budget=${c.AGENT_MAX_BUDGET_USD} "
        f"analysis_turns={c.ANALYSIS_MAX_TURNS} concurrency={c.AGENT_MAX_CONCURRENCY}",
        f"  ask endpoint      {'on (key ' + _mask(c.ASK_API_KEY) + ')' if c.ASK_API_KEY else 'OFF — set ASK_API_KEY'}",
        f"  gitlab            url={c.GITLAB_URL or '-'} ref={c.GITLAB_REF} "
        f"token={_mask(c.GITLAB_TOKEN)} ctx_lines={c.GITLAB_CONTEXT_LINES}",
        f"  gitlab projects   {c.GITLAB_PROJECTS or '-'}",
        f"  debug             log_raw_payload={c.LOG_RAW_PAYLOAD} log_llm_prompt={c.LOG_LLM_PROMPT}",
        "=" * 64,
    ]
    return "\n".join(lines)


def rules_summary(r):
    """Format one chat's effective trigger rules (a dict shaped like DEFAULT_RULES)."""
    statuses = r.get("statuses")
    return "\n".join([
        "ongoing (min gap):        %s" % fmt_duration(r["ongoing_sec"]),
        "critical window:          %s" % fmt_duration(r["critical_window_sec"]),
        "critical error threshold: >%d" % r["critical_threshold"],
        "affected user threshold:  >=%d" % r["affected_user_threshold"],
        "critical rate limit:      1 / %s" % fmt_duration(r["critical_ratelimit_sec"]),
        "stat windows (line 2):    %s" % "/".join(fmt_duration(w) for w in r["stat_windows"]),
        "statuses:                 %s" % ("all" if not statuses else "/".join(
            s for s in c.ALERT_STATUSES if s in statuses)),
    ])


def params_summary(rules=None):
    """Operational parameters (no secrets) for the /params chat command.
    Pass a chat's effective rules to show its overrides; defaults otherwise."""
    return "\n".join([
        rules_summary(rules or c.DEFAULT_RULES),
        "keyword min interval:     %ds" % c.KEYWORD_MIN_INTERVAL_SEC,
        "llm:                      enabled=%s model=%s max_tokens=%d" % (
            c.ENABLE_LLM, c.ANTHROPIC_MODEL, c.ANTHROPIC_MAX_TOKENS),
        "gitlab source:            %s" % ("on" if (c.GITLAB_URL and c.GITLAB_TOKEN and c.GITLAB_PROJECTS) else "off"),
    ])
