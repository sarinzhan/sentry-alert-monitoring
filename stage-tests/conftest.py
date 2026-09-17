"""Shared fixtures for kyc-service stage tests.

Every test here is black-box: it talks to a deployed kyc-service over HTTP the
same way the mobile app does (X-Api-Key + public endpoints). No DB access, no
imports from the service — the observable contract is the only thing asserted.

Configuration comes from environment variables, or a .env file next to this
conftest (see .env.example):

  KYC_BASE_URL                    target service, default http://localhost:9876
  KYC_API_KEY                     X-Api-Key value; empty when auth is disabled
  KYC_TEST_FRESH_MSISDN           msisdn with no KYC history
  KYC_TEST_IDENTIFIED_MSISDN      msisdn of a settled (identified + personified) client
  KYC_TEST_IDENTIFIED_DEVICE_ID   deviceId that settled client is identified with
  KYC_TEST_CLIENT_DATA_FILE       path to JSON with the full anketa ("data" multipart
                                  part) for real IDENTIFY submissions (destructive tests)
"""

import base64
import json
import os
import time
from collections import namedtuple
from pathlib import Path

import pytest
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BASE_URL = os.getenv("KYC_BASE_URL", "http://localhost:9876")
API_KEY = os.getenv("KYC_API_KEY", "").strip()

# 1x1 transparent PNG — a valid image body for file parts.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

IdentifiedClient = namedtuple("IdentifiedClient", "msisdn device_id")


class KycApi:
    """Thin HTTP client mirroring what the mobile frontend can do."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-Api-Key"] = api_key

    def post(self, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        return self.session.post(self.base_url + path, **kwargs)

    def resolve(self, msisdn: str, device_id: str | None = None, *,
                pin: str | None = None, passport_id: str | None = None,
                is_new_device: bool = False) -> requests.Response:
        return self.post("/api/v1/client/resolve", json={
            "msisdn": msisdn,
            "deviceId": device_id,
            "pin": pin,
            "passportId": passport_id,
            "isNewDevice": is_new_device,
        })

    def submit_documents(self, *, action: str, passport_type: str | None = None,
                         data: dict | None = None, device_id: str | None = None,
                         idempotency_key: str | None = None,
                         files: dict | None = None) -> requests.Response:
        headers = {}
        if device_id:
            headers["X-Device-ID"] = device_id
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        form = {"action": action}
        if passport_type:
            form["passportType"] = passport_type
        parts = dict(files or {})
        if data is not None:
            parts["data"] = ("data.json", json.dumps(data), "application/json")
        return self.post("/api/v1/identification/documents",
                         data=form, files=parts, headers=headers, timeout=120)


def wait_until(check, *, timeout: float = 90, interval: float = 5, message: str = "condition"):
    """Polls the public API until `check()` is truthy — the async pipeline makes
    most effects observable only after JMS processing settles."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"timed out after {timeout}s waiting for {message} (last={last!r})")


def _env_or_skip(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"{name} is not set — see stage-tests/.env.example")
    return value


@pytest.fixture(scope="session")
def api() -> KycApi:
    return KycApi(BASE_URL, API_KEY)


@pytest.fixture
def png() -> bytes:
    return PNG_1PX


@pytest.fixture
def fresh_msisdn() -> str:
    return _env_or_skip("KYC_TEST_FRESH_MSISDN")


@pytest.fixture
def identified_client() -> IdentifiedClient:
    return IdentifiedClient(
        msisdn=_env_or_skip("KYC_TEST_IDENTIFIED_MSISDN"),
        device_id=_env_or_skip("KYC_TEST_IDENTIFIED_DEVICE_ID"),
    )


@pytest.fixture
def client_data() -> dict:
    path = Path(_env_or_skip("KYC_TEST_CLIENT_DATA_FILE"))
    if not path.is_file():
        pytest.skip(f"anketa file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
