# sentry-telegram

Receives Sentry webhooks and posts errors to a Telegram chat/forum topic, with an
intentional trigger model (new / ongoing / critical), interactive control from the chat
(mute, per-project mute, keyword force-send), and an optional LLM cause/fix enriched with
the real source, author (git blame) and the diff that last touched the crash line.
All state is in SQLite, so everything survives restarts.

Send `/start` to the bot in the target chat/topic and it replies with the
`chat id` (and `message_thread_id`) to put in `TELEGRAM_CHAT_ID` (use `chatid:thread`).

## Layout

| File | Responsibility |
|---|---|
| `config.py` | `.env` loading, settings, startup `banner()` / `params_summary()` |
| `utils.py` | shared helpers (`esc`) |
| `chat_bot_handler.py` | Telegram app: sending + commands (`/start /help /params /status /mute /mute_project /watch`) |
| `sentry_event_handler.py` | verify, parse, trigger decision, mutes/keywords, format, GitLab, LLM |
| `controller.py` | FastAPI app: endpoints + lifespan wiring |
| `main.py` | entry point |

## Alert triggers

The debounce/spike model was replaced by an ops-driven one. An issue produces an alert when **any** of:

- **🆕 new** — the first time the issue is seen.
- **🔁 ongoing** — at least `WINDOW_INTERVAL_FROM_LAST_ALERT_IN_HOUR` (12h) since the last alert for it.
- **🚨 escalating (critical)** — within `WINDOW_CRITICAL_INTERVAL_IN_MINUTE` (10m) either the
  occurrence count `> CRITICAL_ERROR_THRESHOLD` (15) **or** distinct affected users
  `>= AFFECTED_USER_THRESHOLD` (5); rate-limited to once per `WINDOW_INTERVAL_FOR_CRITICAL_IN_HOUR` (4h).

Precedence: new > critical > ongoing. Affected-user counting needs a user in the event
(`user.ip_address`/`id`/… or the `user` tag). LLM analysis runs **only for escalating alerts in prod**.

## Message format

```
🚨 billing · prod · escalating
@melis · 2025-07-18 15:52 · 2343/43/22 (12h/6h/10m) · #a1b2c3
💬 refactor product dup check

🤖 Likely cause: …
Suggested fix: …

ProductAlreadyConnectedException: Product already connected
Culprit: …SubscriptionServiceImpl in addSubscriptionProduct
level error
SubscriptionServiceImpl.java:519 …
Open in Sentry →
/status a1b2c3  /mute a1b2c3 1  /ai a1b2c3
💰 LLM: $0.0087 · 1423 in / 198 out
```

- **Line 2** = blame author (or mapped `@telegram`) · commit date-time · counts `(windows)` · `#short`.
- **Line 3** = the commit message that last touched the crash line.
- `#short` is a stable 6-hex issue id used by the commands; the `/status` `/mute` `/ai` line is copyable.

## Commands (in the chat)

Sent in the alert chat/topic. The bot polls, so plain commands and replies to alerts work
under Telegram's default privacy mode (no BotFather change).

| Command | What |
|---|---|
| `/help` · `/params` | list commands · current parameter values |
| `/status <id>` | issue state: counts, last alert, mute |
| `/ai <id>` | ask the LLM for cause/fix on demand (reuses cache; works for any alerted issue) |
| `/mute <id> <days>` | snooze an issue (max `MUTE_MAX_DAYS`=7). Or **reply to an alert** with `/mute <days>` |
| `/unmute <id>` · `/muted` | remove an issue mute · list muted issues |
| `/mute_project <project> <days>` | mute a whole project (max `PROJECT_MUTE_MAX_DAYS`=15) |
| `/unmute_project <project>` · `/projects` | remove a project mute · list projects + mute state |
| `/watch add\|del <text> [project]` · `/watched` | keyword force-send (global or per-project) |
| `/map <vcs_author> @<tg>` · `/map del\|list` | map a commit author to a Telegram handle |

`<id>` is the `#short` from line 2. Keyword force-send bypasses debounce and mutes
(set `KEYWORD_MIN_INTERVAL_SEC` > 0 as an anti-spam floor). When a `/map` entry matches the
crash-line author (git blame), line 2 shows the mapped `@telegram` (pinged) instead of the
VCS name.

## LLM cause/fix + GitLab (optional)

When `ENABLE_LLM=true`, escalating prod alerts get a `🤖 cause / fix` from Anthropic. The
prompt is enriched via the Sentry issue's `project_id` → GitLab repo mapping:
current **source** around the crash line, the **author** (git blame), and the **diff** of the
commit that last touched that line. Answers are cached per issue+commit in SQLite (shown as
`💰 cached`) and re-computed only when the code changes. The Anthropic call reuses the same
MITM-tolerant TLS as Telegram (`ANTHROPIC_SSL_INSECURE` / `ANTHROPIC_CA_BUNDLE`).

## Configure (`.env`)

Copy `.env.example` and fill in. Highlights (see `.env.example` for the full list):

| Variable | What |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | bot token; `chatid:thread` target |
| `SENTRY_CLIENT_SECRET` | Internal Integration secret (verifies the webhook; empty = off) |
| `STAT_WINDOWS` | the 3 count windows on line 2 (durations `s/m/h/d`) |
| `WINDOW_*` / `*_THRESHOLD` | the trigger model (see above) |
| `MUTE_MAX_DAYS` / `PROJECT_MUTE_MAX_DAYS` | mute limits |
| `SENTRY_PROJECTS` | project id → display name for the header |
| `GITLAB_URL` / `GITLAB_TOKEN` / `GITLAB_PROJECTS` | GitLab source/blame lookup |
| `ENABLE_LLM` / `ANTHROPIC_*` | LLM cause/fix |
| `TELEGRAM_CA_BUNDLE` / `TELEGRAM_SSL_INSECURE` | Telegram TLS behind a proxy |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | corporate proxy (runtime) |

## Run (Docker, recommended)

```bash
docker compose up -d --build
docker compose logs -f sentry-telegram    # startup banner shows the effective config
```

Secrets come from `.env` next to `docker-compose.yml` (gitignored). The service joins the
Sentry stack's docker network, reachable as `http://sentry-telegram:8080`.

### Or directly

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit it
python main.py            # 0.0.0.0:8080 : POST /webhook, POST /telegram, GET /health
```

## TLS behind a corporate proxy

If the box reaches `api.telegram.org` / `api.anthropic.com` through an intercepting HTTPS
proxy, TLS fails with `CERTIFICATE_VERIFY_FAILED` (self-signed / missing Authority Key
Identifier). The bot builds its own SSL context: it trusts the system store plus an optional
corporate CA (`TELEGRAM_CA_BUNDLE` / `ANTHROPIC_CA_BUNDLE`) and relaxes the strict X.509
check. Last resort: `TELEGRAM_SSL_INSECURE=true` (and `ANTHROPIC_SSL_INSECURE`, which
defaults to the Telegram setting). Internal calls (Sentry API, GitLab) use a proxy-bypassing
client, so add their hosts to `NO_PROXY` on the Sentry stack when it forwards the webhook.

## Wire up Sentry

**Settings → Developer Settings → Custom (Internal) Integration**. Webhook URL
`http://sentry-telegram.local:8080/webhook` (a dotted alias — Sentry's URL validator rejects
single-label hosts), copy the **Client Secret** into `SENTRY_CLIENT_SECRET`, and subscribe to:

- **`error`** — per event, payload **includes the stack trace** (what this tool is built for).
- **`issue`** — lower volume, no stack trace.

Sentry blocks webhooks to private IPs (SSRF); for a self-hosted stack on the docker network,
remove the docker subnet (`172.16.0.0/12` and its IPv6 twin) from `SENTRY_DISALLOWED_IPS` in
`sentry.conf.py`, and add the notifier host to the sender's `NO_PROXY`.

Every `POST /webhook` is logged on arrival, with a warning on bad signature / bad JSON.

## Run as a service (systemd)

`sentry-telegram.service` runs `venv/bin/python main.py`, restarts on failure, runs
non-root under `ProtectSystem=strict` with `state.db` writable via `ReadWritePaths`. Adjust
paths/user to match your install.
