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

Getting `TELEGRAM_CHAT_ID`: add the bot to the chat, send any message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and read the `chat.id`.

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

## How the debounce works

The first time an issue is seen → send immediately. After that, a send only happens when a
webhook actually arrives **and** enough time has passed since the last send for that issue:

```
SEND_WINDOWS = [60, 300]   # gap before 2nd send, then before every later send
```

So per issue: now, then ≥60s later, then ≥300s apart. Anything in between is dropped.
Change the list at the top of `app.py` to retune. The key is the Sentry issue id, so each
issue is throttled independently.

## Run as a service (optional)

```bash
sudo mkdir -p /opt/sentry-telegram && sudo cp *.py .env /opt/sentry-telegram/
cd /opt/sentry-telegram && python3 -m venv venv && ./venv/bin/pip install -r /path/to/requirements.txt
sudo cp sentry-telegram.service /etc/systemd/system/
sudo systemctl enable --now sentry-telegram
```

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
