# sentry-telegram

Receives a Sentry webhook and posts the error to a Telegram chat.
Per-issue debounce (send now → ≥1 min → every 5 min) so a spiking error can't flood the channel.
State is in SQLite, so the debounce survives restarts.

Send `/start` to the bot in any chat (or forum topic) and it replies with the
chat id (and topic `message_thread_id`) to put in `TELEGRAM_CHAT_ID`.

## Layout

| File | Responsibility |
|---|---|
| `config.py` | `.env` loading, settings, logging |
| `utils.py` | shared helpers (`esc`) |
| `chat_bot_handler.py` | `ChatBotHandler` — python-telegram-bot app, `/start`, sending |
| `sentry_event_handler.py` | `SentryEventHandler` — verify, parse, debounce, format, LLM |
| `controller.py` | FastAPI app: endpoints + lifespan wiring the two handlers |
| `main.py` | entry point |

## Run on a server

```bash
pip install -r requirements.txt
cp .env.example .env        # then edit it
python main.py
```

That starts the listener on `0.0.0.0:8080`. Endpoints: `POST /webhook` (Sentry),
`POST /telegram` (Telegram webhook, alternative to polling), and `GET /health`.
Put it behind your reverse proxy / TLS as usual.

## Configure (`.env`)

| Variable | Required | What |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `TELEGRAM_CHAT_ID` | yes | target chat/channel id (negative for groups/channels) |
| `SENTRY_CLIENT_SECRET` | recommended | Internal Integration Client Secret; verifies the request. Empty = check off |
| `HOST` / `PORT` | no | default `0.0.0.0` / `8080` |
| `DB_PATH` | no | default `state.db` |
| `TELEGRAM_CA_BUNDLE` | no | PEM CA bundle to trust for `api.telegram.org` (see TLS below) |
| `TELEGRAM_SSL_INSECURE` | no | `true` skips TLS verification (last resort); default `false` |

Getting `TELEGRAM_CHAT_ID`: add the bot to the chat, send any message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and read the `chat.id`.

## TLS behind a corporate proxy

If the box reaches `api.telegram.org` through an intercepting HTTPS proxy (common on
corporate networks — httpx picks the proxy up from `HTTPS_PROXY`/`https_proxy`), startup
can fail with:

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier
```

The proxy presents a certificate signed by an internal CA, and modern OpenSSL rejects it.
The bot builds its own SSL context to handle this:

- it trusts the system/certifi store **plus** `TELEGRAM_CA_BUNDLE` if set — point that at
  the corporate CA (PEM) so the proxy cert verifies, and
- it relaxes the strict X.509 check that flags a CA cert with no Authority Key Identifier
  (the cause of the error above).

Preferred fix: `TELEGRAM_CA_BUNDLE=/etc/ssl/certs/corporate-ca.pem`. As a last resort when
you can't obtain a clean CA cert, set `TELEGRAM_SSL_INSECURE=true` to skip verification.

## Wire up Sentry

In Sentry: **Settings → Developer Settings → Internal Integration**. Set the webhook URL to
`https://your-host/webhook`, copy the **Client Secret** into `SENTRY_CLIENT_SECRET`, and
subscribe to one of:

- **`issue`** — fires on new issue / regression. Lower volume, but the payload has **no
  stack trace** (you get title, culprit, type, message, link).
- **`error`** — fires per event, so the payload **includes the stack trace** (richer
  message). Higher webhook volume, but the debounce collapses it to your
  now / ≥1 min / every 5 min schedule per issue.

The script handles both shapes automatically — pick based on whether you want stack
frames in the message.

Every `POST /webhook` call is logged on arrival (`/webhook called: resource=... bytes=...
from=...`), with a warning on a rejected signature or unparseable body — handy for
confirming Sentry is actually reaching the listener.

## How the debounce works

The first time an issue is seen → send immediately. After that, a send only happens when a
webhook actually arrives **and** enough time has passed since the last send for that issue:

```
SEND_WINDOWS = [60, 300]   # gap before 2nd send, then before every later send
```

So per issue: now, then ≥60s later, then ≥300s apart. Anything in between is dropped.
Change `SEND_WINDOWS` in `config.py` to retune. The key is the Sentry issue id, so each
issue is throttled independently.

## Run as a service (systemd)

```bash
# 1. copy the project and config into place
sudo mkdir -p /opt/sentry-telegram
sudo cp *.py requirements.txt .env /opt/sentry-telegram/

# 2. create a venv and install deps
cd /opt/sentry-telegram
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt

# 3. create an unprivileged user and hand it the directory
sudo useradd --system --no-create-home --shell /usr/sbin/nologin sentry-telegram
sudo chown -R sentry-telegram:sentry-telegram /opt/sentry-telegram

# 4. install and start the unit
sudo cp sentry-telegram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentry-telegram
```

Check it:

```bash
systemctl status sentry-telegram
journalctl -u sentry-telegram -f      # follow logs
```

The unit (`sentry-telegram.service`) runs `venv/bin/python main.py` from
`/opt/sentry-telegram`, restarts on failure, and is locked down (non-root,
`ProtectSystem=strict`); `state.db` stays writable via `ReadWritePaths`.
If you change paths or the username, edit the unit to match.

## Future: LLM cause + fix

Already stubbed in `analyze()` and off by default. It calls the Anthropic API over `httpx`
(no extra dependency to add). To turn on later, set in `.env`:

```
ENABLE_LLM=true
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6
```

When on, a short "🤖 likely cause / suggested fix" section is added to the message.
Note: it runs before the send, so it adds a moment of latency — if you'd rather keep the
alert instant, the cleanest change is to send the main message first and post the analysis
as a follow-up reply.
