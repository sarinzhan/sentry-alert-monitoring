"""Black-box tests for POST /api/v1/client/resolve — the entry point of every KYC
journey: the mobile app calls it on login and renders whatever nextAction says."""

import uuid

import pytest

from flows import Flow, Step

# The full scenario -> nextAction contract of the API (Scenario.java). If the
# service ever returns a pair outside this table, the mobile app is broken.
SCENARIO_NEXT_ACTION = {
    "NEW_BEELINE_USER": "NEW_USER",
    "BEELINE_PERSONIFICATION": "IDENTIFY",
    "BALANCE_IDENTIFICATION": "IDENTIFY",
    "PERSONIFIED_NO_IDENT": "OFFER_IDENTIFICATION",
    "PERSONIFIED_NO_IDENT_FOREIGN": "OK_FOREIGN",
    "PERSONIFICATION_AFTER_REJECT": "OFFER_PERSONIFICATION_AFTER_REJECT",
    "OTHER_OPERATOR_IDENT": "FACE_CHECK",
    "OTHER_OPERATOR_NO_IDENT": "IDENTIFY",
    "FACE_CHECK": "FACE_CHECK",
    "MANUAL_OFFICE": "REDIRECT_TO_OFFICE",
    "AWAIT": "AWAIT",
    "OK": "OK",
    "OK_IDENTIFICATION_LIMIT_EXCEEDED": "OK_IDENTIFICATION_LIMIT_EXCEEDED",
}


@pytest.mark.flow(Flow.IDENTIFICATION_PERSONIFICATION, step=Step.RESOLVE)
class TestResolveContract:

    @pytest.mark.needs("KYC_TEST_FRESH_MSISDN — msisdn without KYC history")
    def test_returns_known_scenario_and_matching_next_action(self, api, fresh_msisdn):
        """Resolve for a client with no KYC history must answer 200 with a
        scenario the app knows, and nextAction must match that scenario's
        contract (the app only reads nextAction — a mismatched pair would
        silently send the user down the wrong journey)."""
        r = api.resolve(fresh_msisdn, device_id="stage-tests-device")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["scenario"] in SCENARIO_NEXT_ACTION, body
        assert body["nextAction"] == SCENARIO_NEXT_ACTION[body["scenario"]], body

    @pytest.mark.needs("KYC_TEST_FRESH_MSISDN — msisdn without KYC history")
    def test_message_is_localized_by_accept_language(self, api, fresh_msisdn):
        """The scenario message comes from the message bundle keyed by
        Accept-Language; both ru and en must yield a non-empty message
        (an empty one means a missing bundle key on stage)."""
        for lang in ("ru", "en"):
            r = api.post("/api/v1/client/resolve",
                         json={"msisdn": fresh_msisdn, "deviceId": "stage-tests-device"},
                         headers={"Accept-Language": lang})
            assert r.status_code == 200, r.text
            assert r.json().get("message"), f"empty message for Accept-Language={lang}"


@pytest.mark.flow(Flow.IDENTIFICATION_PERSONIFICATION, step=Step.RESOLVE)
class TestResolveDeviceChange:

    @pytest.mark.needs("KYC_TEST_IDENTIFIED_MSISDN / KYC_TEST_IDENTIFIED_DEVICE_ID — settled client")
    def test_settled_client_on_known_device_is_ok(self, api, identified_client):
        """An identified + personified client on the device they identified with
        has nothing left to do — the app must get OK, not a re-identification
        or face-check prompt."""
        r = api.resolve(identified_client.msisdn, device_id=identified_client.device_id)
        assert r.status_code == 200, r.text
        assert r.json()["nextAction"] == "OK", r.json()

    @pytest.mark.needs("KYC_TEST_IDENTIFIED_MSISDN — settled client")
    def test_new_device_requires_face_check(self, api, identified_client):
        """The same settled client appearing on an unknown device must be sent
        to FACE_CHECK (selfie match) — this is the anti-takeover gate; OK here
        would mean a stolen SIM gets full access without a face match."""
        r = api.resolve(identified_client.msisdn,
                        device_id=f"stage-tests-{uuid.uuid4().hex[:12]}",
                        is_new_device=True)
        assert r.status_code == 200, r.text
        assert r.json()["nextAction"] == "FACE_CHECK", r.json()
