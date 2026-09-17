# stage-tests

Black-box API tests against the deployed **kyc-service** stage, plus the testops
backend that `../testops-ui` talks to.

Tests act exactly like the mobile frontend: HTTP + `X-Api-Key`, assert on
responses only (async pipeline effects are observed by polling `/resolve`).
No service imports, no DB access.

## Layout

```
flows.py             flow/step slugs — the only flow data in code
conftest.py          env config + KycApi client + fixtures
identification/      tests, linked to flow steps via @pytest.mark.flow
collect_tests.py     dumps test inventory (node id, doc, flow link) as JSON
server.py            FastAPI: inventory, run tests, flow catalog CRUD (testops.db)
```

Flow/step **identity** lives in code; **titles, descriptions, ordering and
Sentry signal patterns** live in `testops.db` and are edited from the UI.
Slugs seen in test markers are auto-registered on collection.

## Setup

```bash
cd stage-tests
pip install -r requirements.txt
cp .env.example .env          # fill in stage URL, api key, test msisdns
```

Test data needed on stage (see `.env.example`):
- a **fresh msisdn** (no KYC history) — contract tests on `/resolve`
- a **settled client** (identified + personified) and their known deviceId —
  device-change / FACE_CHECK tests
- an **anketa JSON** (`client-data.example.json` as template) — only for
  `destructive` tests that create real applications

## Run from CLI

```bash
pytest                        # all non-destructive tests
pytest -m ""                  # include destructive (real submissions, rate limits!)
pytest identification/test_resolve.py -v
```

Tests missing their env vars **skip** with a reason, they don't fail.

## Run the testops backend (for the UI)

```bash
uvicorn server:app --port 8077
```

API: `GET /api/tests`, `POST /api/runs`, `GET /api/runs/{id}`,
`GET /api/flows`, `PUT /api/flows/{f}/steps/{s}`.
Runs selected from the UI bypass the `not destructive` default filter —
destructive tests execute only when explicitly picked.

The flow catalog (`GET /api/flows`) is also the integration point for
sentry-monit: each step carries `signals` (loggers, endpoints, queues, kafka
topics) to match Sentry events onto flow steps.
