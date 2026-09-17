"""TestOps backend for testops-ui: pytest inventory + on-demand runs + flow catalog.

- Flow/step *identity* comes from code (flows.py markers on tests) and is
  auto-registered here on collection; titles/descriptions/signals are CRUD-only
  (edited from the UI, stored in testops.db).
- Runs execute pytest in a subprocess with pytest-json-report; selected node ids
  bypass the default `-m "not destructive"` filter, so destructive tests run
  only when explicitly picked.

Start:  uvicorn server:app --port 8077   (from this directory)
"""

import json
import sqlite3
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "testops.db"
RUN_TIMEOUT_SEC = 1800

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows(
  slug TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS steps(
  flow_slug TEXT NOT NULL REFERENCES flows(slug),
  slug TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0,
  signals TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (flow_slug, slug)
);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  node_ids TEXT NOT NULL,
  status TEXT NOT NULL,
  report TEXT
);
"""

# One-time seed of the first flow. INSERT OR IGNORE: the UI owns this content
# afterwards — reseeding never overwrites edits.
SEED_FLOW = ("identification-personification",
             "Identification → Personification",
             "Full journey of a new SuperApp user: scenario resolve, document "
             "submission, async AML + photo validation pipeline, personification "
             "emitted to configurator-service via Kafka.")
SEED_STEPS = [
    ("resolve", "Scenario resolve", 1,
     "App calls resolve on login; response nextAction drives the UI "
     "(NEW_USER / IDENTIFY / FACE_CHECK / AWAIT / OK...).",
     {"service": "kyc-service", "endpoints": ["/api/v1/client/resolve"],
      "loggers": ["ScenarioResolver", "ClientController"]}),
    ("submit-documents", "Submit documents", 2,
     "Passport photos + selfie + anketa via multipart; validated, persisted as "
     "IN_PROGRESS, files uploaded, enqueued to JMS.",
     {"service": "kyc-service", "endpoints": ["/api/v1/identification/documents"],
      "loggers": ["IdentificationSubmitter", "KYCController"]}),
    ("aml-check", "AML screening", 3,
     "Async: account-limit check + AML verification; REJECTED cancels the "
     "identification.",
     {"service": "kyc-service", "queues": ["new-user-identification-queue"],
      "loggers": ["IdentificationProcessorImpl", "AmlVerificationServiceImpl"]}),
    ("photo-validation", "Photo validation", 4,
     "Async: gov-service /identity + similarity×liveness matrix; PROCEED → "
     "IDENTIFICATION_READY, else AWAITING_VERIFICATION (manual).",
     {"service": "kyc-service", "queues": ["photo-verify-queue"],
      "loggers": ["PhotoValidationProcessorImpl", "FaceCheckServiceImpl"]}),
    ("personification", "Personification", 5,
     "On IDENTIFICATION_READY: PDF forms generated + PersonificationPayload "
     "published to Kafka; configurator-service consumes it.",
     {"service": "kyc-service → configurator-service",
      "kafka": ["superapp-personification"],
      "loggers": ["ClientStatusServiceImpl", "KafkaProducer"]}),
]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO flows(slug, title, description) VALUES (?,?,?)",
                     SEED_FLOW)
        for slug, title, position, description, signals in SEED_STEPS:
            conn.execute(
                "INSERT OR IGNORE INTO steps(flow_slug, slug, title, description, position, signals)"
                " VALUES (?,?,?,?,?,?)",
                (SEED_FLOW[0], slug, title, description, position, json.dumps(signals)))


init_db()

app = FastAPI(title="stage-testops")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class StepUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    signals: dict | None = None
    position: int | None = None


class FlowUpdate(BaseModel):
    title: str | None = None
    description: str | None = None


class RunRequest(BaseModel):
    node_ids: list[str]


def _collect() -> list[dict]:
    proc = subprocess.run([sys.executable, str(ROOT / "collect_tests.py")],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise HTTPException(500, f"test collection failed: {proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def _register_slugs(tests: list[dict]) -> None:
    """Flow/step slugs seen in markers appear in the catalog automatically as
    bare skeletons; the UI enriches them."""
    with db() as conn:
        for t in tests:
            if not t["flow"]:
                continue
            conn.execute("INSERT OR IGNORE INTO flows(slug) VALUES (?)", (t["flow"],))
            if t["step"]:
                pos = conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM steps WHERE flow_slug=?",
                    (t["flow"],)).fetchone()[0]
                conn.execute(
                    "INSERT OR IGNORE INTO steps(flow_slug, slug, position) VALUES (?,?,?)",
                    (t["flow"], t["step"], pos))


@app.get("/api/tests")
def list_tests():
    tests = _collect()
    _register_slugs(tests)
    return tests


@app.get("/api/flows")
def list_flows():
    with db() as conn:
        flows = [dict(r) for r in conn.execute("SELECT * FROM flows ORDER BY slug")]
        for flow in flows:
            flow["steps"] = [
                {**dict(r), "signals": json.loads(r["signals"])}
                for r in conn.execute(
                    "SELECT * FROM steps WHERE flow_slug=? ORDER BY position",
                    (flow["slug"],))
            ]
    return flows


@app.put("/api/flows/{flow_slug}")
def update_flow(flow_slug: str, body: FlowUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "nothing to update")
    with db() as conn:
        cur = conn.execute(
            f"UPDATE flows SET {', '.join(f'{k}=?' for k in fields)} WHERE slug=?",
            (*fields.values(), flow_slug))
    if cur.rowcount == 0:
        raise HTTPException(404, "unknown flow")
    return {"ok": True}


@app.put("/api/flows/{flow_slug}/steps/{step_slug}")
def update_step(flow_slug: str, step_slug: str, body: StepUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "nothing to update")
    if "signals" in fields:
        fields["signals"] = json.dumps(fields["signals"])
    with db() as conn:
        cur = conn.execute(
            f"UPDATE steps SET {', '.join(f'{k}=?' for k in fields)} WHERE flow_slug=? AND slug=?",
            (*fields.values(), flow_slug, step_slug))
    if cur.rowcount == 0:
        raise HTTPException(404, "unknown step")
    return {"ok": True}


def _execute_run(run_id: str, node_ids: list[str]) -> None:
    report_file = ROOT / f"run-{run_id}.json"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *node_ids,
             "--override-ini=addopts=", "-q",
             "--json-report", f"--json-report-file={report_file}"],
            cwd=ROOT, capture_output=True, text=True, timeout=RUN_TIMEOUT_SEC)
        if report_file.is_file():
            raw = json.loads(report_file.read_text(encoding="utf-8"))
            report = {
                "summary": raw.get("summary", {}),
                "duration": raw.get("duration"),
                "tests": [{
                    "node_id": t["nodeid"],
                    "outcome": t["outcome"],
                    "duration": round(sum(t.get(ph, {}).get("duration", 0)
                                          for ph in ("setup", "call", "teardown")), 2),
                    "message": (t.get("call") or t.get("setup") or {}).get("longrepr", ""),
                } for t in raw.get("tests", [])],
            }
            status = "passed" if proc.returncode == 0 else "failed"
        else:
            report = {"error": (proc.stderr or proc.stdout)[-4000:]}
            status = "error"
    except Exception as exc:  # noqa: BLE001 — a run must never leave status=running
        report, status = {"error": repr(exc)}, "error"
    finally:
        report_file.unlink(missing_ok=True)
    with db() as conn:
        conn.execute("UPDATE runs SET status=?, report=? WHERE id=?",
                     (status, json.dumps(report), run_id))


@app.post("/api/runs")
def start_run(body: RunRequest):
    if not body.node_ids:
        raise HTTPException(400, "node_ids is empty")
    run_id = uuid.uuid4().hex[:12]
    with db() as conn:
        conn.execute("INSERT INTO runs(id, created_at, node_ids, status) VALUES (?,?,?,?)",
                     (run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      json.dumps(body.node_ids), "running"))
    threading.Thread(target=_execute_run, args=(run_id, body.node_ids), daemon=True).start()
    return {"id": run_id}


@app.get("/api/runs")
def list_runs():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, node_ids, status FROM runs "
            "ORDER BY created_at DESC LIMIT 50").fetchall()
    return [{**dict(r), "node_ids": json.loads(r["node_ids"])} for r in rows]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown run")
    out = {**dict(row), "node_ids": json.loads(row["node_ids"])}
    out["report"] = json.loads(row["report"]) if row["report"] else None
    return out
