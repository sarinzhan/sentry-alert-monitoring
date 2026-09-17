"""Black-box tests for POST /api/v1/identification/documents — the mobile app
submits passport photos + selfie + anketa here; everything after the 200 happens
asynchronously (JMS -> AML -> photo validation -> Kafka), so downstream effects
are asserted only through what the frontend can see: /resolve."""

import json
import uuid

import pytest
import requests

from conftest import API_KEY, BASE_URL, wait_until
from flows import Flow, Step


def _image_parts(png: bytes) -> dict:
    return {
        "passport_front": ("front.png", png, "image/png"),
        "passport_back": ("back.png", png, "image/png"),
        "selfie_with_passport": ("selfie.png", png, "image/png"),
    }


@pytest.mark.flow(Flow.IDENTIFICATION_PERSONIFICATION, step=Step.SUBMIT_DOCUMENTS)
class TestDocumentsRequestContract:

    def test_unknown_action_is_client_error(self, api, png):
        """`action` binds to the Actions enum — garbage must fail request
        binding as a 4xx, never a 500 (a 500 here pages on-call for what is
        a malformed client request)."""
        r = api.submit_documents(action="NOT_A_REAL_ACTION",
                                 files=_image_parts(png))
        assert 400 <= r.status_code < 500, f"{r.status_code}: {r.text[:300]}"

    def test_unknown_passport_type_is_client_error(self, api, png):
        """Same binding contract for `passportType` (KG_ID / FOREIGN_ID /
        FOREIGN_PASSPORT / DIGITAL_TUNDUK are the only valid values)."""
        r = api.submit_documents(action="IDENTIFY", passport_type="MARS_ID",
                                 files=_image_parts(png))
        assert 400 <= r.status_code < 500, f"{r.status_code}: {r.text[:300]}"

    @pytest.mark.skipif(not API_KEY, reason="KYC_API_KEY unset — target runs with auth disabled")
    def test_rejects_request_without_api_key(self, png):
        """With security.api-key set on stage, a request without X-Api-Key must
        be rejected — this endpoint accepts identity documents."""
        r = requests.post(f"{BASE_URL}/api/v1/identification/documents",
                          data={"action": "IDENTIFY"},
                          files=_image_parts(png), timeout=30)
        assert r.status_code in (401, 403), f"{r.status_code}: {r.text[:300]}"


@pytest.mark.destructive
@pytest.mark.flow(Flow.IDENTIFICATION_PERSONIFICATION, step=Step.SUBMIT_DOCUMENTS)
class TestIdentifySubmission:
    """Real IDENTIFY submissions: they create applications on stage, hit the
    real gov-service/AML integrations and burn the per-msisdn rate limit —
    hence `destructive` (run only when explicitly selected)."""

    @pytest.mark.needs("KYC_TEST_CLIENT_DATA_FILE — real anketa JSON for a test person")
    def test_identify_is_accepted_and_resolve_flips_to_await(self, api, client_data, png):
        """Happy-path entry into the pipeline: submit a full IDENTIFY
        application, expect 200 with an action/status for the app, then the
        only frontend-observable effect — /resolve for that msisdn showing
        AWAIT while the async pipeline (AML -> photo validation) works."""
        device_id = f"stage-tests-{uuid.uuid4().hex[:12]}"
        r = api.submit_documents(action="IDENTIFY", passport_type="KG_ID",
                                 data=client_data, device_id=device_id,
                                 idempotency_key=str(uuid.uuid4()),
                                 files=_image_parts(png))
        assert r.status_code == 200, f"{r.status_code}: {r.text[:500]}"
        body = r.json()
        assert body.get("action") or body.get("status"), body

        wait_until(
            lambda: api.resolve(client_data["phone"], device_id=device_id)
                       .json().get("nextAction") == "AWAIT",
            timeout=60, interval=5,
            message="resolve to show AWAIT after IDENTIFY submission",
        )

    @pytest.mark.needs("KYC_TEST_CLIENT_DATA_FILE — real anketa JSON for a test person")
    def test_same_idempotency_key_is_safe_to_retry(self, api, client_data, png):
        """The app retries on flaky mobile networks with the same
        X-Idempotency-Key; the retry must succeed and answer the same
        action/status instead of creating a second application or failing."""
        key = str(uuid.uuid4())
        device_id = f"stage-tests-{uuid.uuid4().hex[:12]}"
        submit = lambda: api.submit_documents(
            action="IDENTIFY", passport_type="KG_ID", data=client_data,
            device_id=device_id, idempotency_key=key, files=_image_parts(png))

        first, second = submit(), submit()
        assert first.status_code == 200, first.text[:500]
        assert second.status_code == 200, second.text[:500]
        a, b = first.json(), second.json()
        assert (a.get("action"), a.get("status")) == (b.get("action"), b.get("status")), (a, b)

    @pytest.mark.needs("KYC_TEST_CLIENT_DATA_FILE — real anketa JSON for a test person")
    def test_rate_limit_answers_429_with_retry_after(self, api, client_data, png):
        """action=IDENTIFY is rate-limited per msisdn; when the limit is hit the
        contract is 429 + Retry-After header (the app shows a cooldown timer).
        Submits until 429 (capped) — inconclusive if the stage limit is higher
        than the cap."""
        for _ in range(6):
            r = api.submit_documents(action="IDENTIFY", passport_type="KG_ID",
                                     data=client_data,
                                     device_id=f"stage-tests-{uuid.uuid4().hex[:12]}",
                                     idempotency_key=str(uuid.uuid4()),
                                     files=_image_parts(png))
            if r.status_code == 429:
                assert r.headers.get("Retry-After"), "429 without Retry-After header"
                return
        pytest.skip("rate limit not reached in 6 attempts — stage limit is higher than the test cap")
