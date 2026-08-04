# sentry-telegram

Receives Sentry webhooks and posts errors to **any** Telegram chat/forum topic that
subscribes, with an intentional trigger model (new / ongoing / critical), interactive
control from the chat (per-chat subscriptions, rules, keyword force-send), and an optional LLM
cause/fix enriched with the real source, author (git blame) and the diff that last touched
the crash line. All state is in SQLite, so everything survives restarts.

## Subscriptions (per chat, opt-in)

Add the bot to any chat/topic. **By default it sends nothing** — each chat opts in:

- `/subscribe <project|all>` — receive alerts for a project (numeric id, name, or `all`).
  `/unsubscribe …`, `/subscriptions`, and `/projects` (all projects, ✅ = subscribed).
- `/alerts <new ongoing escalating | all>` — which statuses this chat wants (default: all).
- `/set <param> <value>` — **this chat's own** trigger rules (`/set reset` to clear,
  `/params` to view). Every chat has its own rules and its own send state.

There is **no default/global chat** — the bot only posts where a chat has subscribed. Add
the bot to a chat/topic, send `/start` for a quick guide, then `/subscribe`.

## Layout

The code is a layered `app/` package. Dependencies point one way:
`commands → deps → {repositories, services}` and `pipeline → {repositories, services}`.

```
main.py                     entry point
app/
  config.py                 .env loading + settings (pure data)
  summaries.py              banner() / params_summary() / duration parsing
  utils.py                  esc(), short_id()
  controller.py             FastAPI endpoints + lifespan = composition root (wires everything)
  db.py                     Database: the single sqlite connection + all schema/migrations
  repositories/             thin data access, one module per table-concern
    issues, subscriptions, rules, chat_state, keywords, usermap, context
  services/                 external I/O clients
    sentry_api (id→name) · gitlab (source/blame/diff) · llm (Anthropic)
  sentry/                   webhook domain
    parser · security · decision (trigger state machine) · message · analysis · pipeline
  telegram/
    bot.py                  Application lifecycle, sending, webhook clearing (409 fix)
    formatting.py           HELP_TEXT + display formatters
    deps.py                 dependency bundle injected into bot_data
    commands/               one file per command group (subscribe, alerts, set, status, ai, watch, map, …)
```

Each Telegram command lives in its own module under `app/telegram/commands/` and reads
its data/service dependencies from the injected `Deps` bundle. The composition root in
`app/controller.py` builds `Database → repositories → services → pipeline → bot` and
injects them, so no layer reaches back up.

## Alert triggers

The debounce/spike model was replaced by an ops-driven one. An issue produces an alert when **any** of:

- **🆕 new** — the first time the issue is seen.
- **🔁 ongoing** — at least `WINDOW_INTERVAL_FROM_LAST_ALERT_IN_HOUR` (12h) since the last alert for it.
- **🚨 escalating (critical)** — within `WINDOW_CRITICAL_INTERVAL_IN_MINUTE` (10m) either the
  occurrence count `> CRITICAL_ERROR_THRESHOLD` (15) **or** distinct affected users
  `>= AFFECTED_USER_THRESHOLD` (5); rate-limited to once per `WINDOW_INTERVAL_FOR_CRITICAL_IN_HOUR` (4h).

Precedence: new > critical > ongoing. Affected-user counting needs a user in the event
(`user.ip_address`/`id`/… or the `user` tag). LLM analysis runs **only for escalating alerts in prod**.

These are **defaults**. Each subscribed chat can override its own thresholds, windows and
which statuses it receives (see the `/set` / `/alerts` commands below); occurrence counts
are global (per issue), but the ongoing gap and critical rate-limit are tracked per chat.

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
/status a1b2c3  /ai a1b2c3
💰 LLM: $0.0087 · 1423 in / 198 out
```

- **Line 2** = blame author (or mapped `@telegram`) · commit date-time · counts `(windows)` · `#short`.
- **Line 3** = the commit message that last touched the crash line.
- `#short` is a stable 6-hex issue id used by the commands; the `/status` `/ai` line is copyable.

## Commands (in the chat)

Sent in the alert chat/topic. The bot polls, so plain commands and replies to alerts work
under Telegram's default privacy mode (no BotFather change).

| Command | What |
|---|---|
| `/help` · `/params` | list commands · **this chat's** effective parameter values |
| `/subscribe <project\|all>` · `/unsubscribe <…>` | opt this chat in/out of a project's alerts |
| `/subscriptions` · `/projects` | this chat's subscriptions · all projects (✅ = subscribed) |
| `/alerts <new ongoing escalating\|all>` | which statuses this chat receives (default all) |
| `/set <param> <value>` · `/set reset` | this chat's rules: `ongoing`, `critical_window`, `critical_threshold`, `affected_users`, `critical_ratelimit`, `stat_windows` |
| `/status <id>` | issue state: counts, last alert |
| `/ai <id>` | ask the LLM for cause/fix on demand (reuses cache; works for any alerted issue) |
| `/req <request_id> [period]` | all events of one request across services (Discover lookup) |
| `/why <msisdn> <time> <descr>` | why a subscriber hit an error: business logic vs code bug (agentic LLM) |
| `/activity <msisdn> [1\|3\|6]` | the subscriber's error timeline over the last N hours + LLM summary |
| `/api <path>` | endpoint documentation generated from the GitLab code: curl, contract, behavior |
| `/llm <id>` | inspect a saved LLM call (prompt, tools used, tokens) by the `llm_…` id under a reply |
| `/watch add\|del <text> [project]` · `/watched` | keyword force-send (global or per-project) |
| `/map <vcs_author> @<tg>` · `/map del\|list` | map a commit author to a Telegram handle |

To stop alerts, a chat simply `/unsubscribe`s the project or narrows `/alerts` — there is no
per-issue mute. `<id>` is the `#short` from line 2. Keyword force-send bypasses the per-chat
min gap and status filter (set `KEYWORD_MIN_INTERVAL_SEC` > 0 as an anti-spam floor). When a
`/map` entry matches the crash-line author (git blame), line 2 shows the mapped `@telegram`
(pinged) instead of the VCS name.

## Investigation commands (Sentry Discover + agentic LLM)

These need `SENTRY_API_TOKEN` (with `event:read`) so the bot can run Discover
queries; the search keys are configurable because they depend on how the
services tag events:

- **`/req <request_id> [period]`** — every event of one request across all
  services. Tries each key in `SENTRY_REQUEST_ID_FIELDS` (default
  `request_id,trace`) until one hits. Also `GET /api/request/{id}?period=24h`.
- **`/why <msisdn> <time> <description>`** — searches the subscriber's errors
  ±45 min around the given local time (auto-widens to ±3 h), feeds them with
  full details to the agentic LLM (GitLab tools + Sentry `user_events` /
  `event_details` / `related_errors`) and answers with a verdict: **business
  logic** (system worked as designed) or **code error**, plus evidence.
  msisdn search keys: `SENTRY_MSISDN_FIELDS` (default
  `user.id,user.username,msisdn`); local-time offset: `TIMEZONE_OFFSET_HOURS`.
- **`/activity <msisdn> [1|3|6]`** — the subscriber's timeline over the last
  N hours: application **log lines** (Sentry logs dataset — successful actions
  included, found full-text via `SENTRY_LOGS_MSISDN_QUERY`) merged with error
  events, plus an LLM summary of what they did and what failed. `/why` uses the
  same log lookup, and the agent gets a `search_logs` tool.
- **`/api <path>`** — API documentation generated from the mapped GitLab repos:
  the agentic LLM finds the endpoint (case-insensitive, typo-tolerant), reads
  the handler and replies with the contract, a curl example and the behavior;
  when no single endpoint matches it lists the closest candidates instead.
  Cached per query; `/api refresh <path>` regenerates.

## LLM audit trail

Every LLM call is persisted in full (prompt, tools offered, each tool
invocation with args and result preview, tokens, cost, duration, response) in
the `llm_call` table, and every LLM-backed reply carries its id
(`🔍 /llm llm_a1b2c3`). `/llm <id>` shows a summary in the chat;
`GET /api/llm/{id}` returns the complete record as JSON. Cached `/ai` answers
keep the id of the call that produced them.

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
| `TELEGRAM_BOT_TOKEN` | bot token (the only required var; chats subscribe at runtime) |
| `SENTRY_CLIENT_SECRET` | Internal Integration secret (verifies the webhook; empty = off) |
| `STAT_WINDOWS` | the 3 count windows on line 2 (durations `s/m/h/d`) |
| `WINDOW_*` / `*_THRESHOLD` | the trigger model defaults (each chat can override) |
| `SENTRY_PROJECTS` | project id → display name for the header |
| `GITLAB_URL` / `GITLAB_TOKEN` / `GITLAB_PROJECTS` | GitLab source/blame lookup |
| `SENTRY_MSISDN_FIELDS` / `SENTRY_REQUEST_ID_FIELDS` | Discover search keys for `/why`,`/activity` / `/req` |
| `TIMEZONE_OFFSET_HOURS` | local-time offset for times typed in `/why` (default +6) |
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

**Telegram 409 `Conflict` (getUpdates vs webhook).** `getUpdates` (long-polling) and a
Telegram webhook can't both be active. On startup the bot deletes any registered webhook
before polling (verified with a short retry loop). If you intentionally run the `/telegram`
webhook instead, set `TELEGRAM_POLLING=false` so it never calls `getUpdates`. A stubborn 409
usually means a **second instance** is polling the same bot token — stop the duplicate.

## Run as a service (systemd)

`sentry-telegram.service` runs `venv/bin/python main.py`, restarts on failure, runs
non-root under `ProtectSystem=strict` with `state.db` writable via `ReadWritePaths`. Adjust
paths/user to match your install.
