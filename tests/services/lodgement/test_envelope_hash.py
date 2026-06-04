"""Round-trip: bytes → b64 + sha256 → request body shape matches the contract.

Captures the actual JSON the service POSTs and asserts it has the
exact field names, sha256 of the input bytes, and base64 that
round-trips to the original.
"""
from __future__ import annotations

import base64
import hashlib

import httpx
import respx

from saebooks.services.lodgement import RemoteLodgementService
from tests.services.lodgement.conftest import TEST_BASE_URL, TEST_TOKEN

ENVELOPE = b"<STP><Header/><Payload>foo</Payload></STP>"


@respx.mock
async def test_request_body_matches_contract_shape() -> None:
    route = respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "accepted",
                "ato_receipt_id": "R1",
                "ato_timestamp": "2026-05-02T12:00:00Z",
                "warnings": [],
            },
        )
    )

    svc = RemoteLodgementService(submitter_abn="12345678901")
    await svc.lodge_stp(
        envelope=ENVELOPE,
        payevent_id="payevent-id-uuid-here",
        metadata={"pay_period_end": "2026-04-30", "employee_count": 7},
    )

    assert route.called
    sent = route.calls.last.request
    body = sent.read()
    import json

    payload = json.loads(body)

    # Field names exactly per contract:
    assert set(payload.keys()) == {
        "envelope_xml",
        "envelope_hash",
        "submitter_abn",
        "payevent_id",
        "metadata",
    }

    # sha256 + base64 derived from the same input bytes:
    assert payload["envelope_hash"] == hashlib.sha256(ENVELOPE).hexdigest()
    assert base64.b64decode(payload["envelope_xml"]) == ENVELOPE

    # Pass-through fields:
    assert payload["submitter_abn"] == "12345678901"
    assert payload["payevent_id"] == "payevent-id-uuid-here"
    assert payload["metadata"]["pay_period_end"] == "2026-04-30"
    assert payload["metadata"]["employee_count"] == 7

    # Authorization header carries the licence token verbatim:
    assert sent.headers["authorization"] == f"Bearer {TEST_TOKEN}"


@respx.mock
async def test_bas_uses_period_id_under_payevent_id_field() -> None:
    """Contract uses ``payevent_id`` for every envelope route's idempotency key."""
    route = respx.post(f"{TEST_BASE_URL}/api/v1/bas/lodge").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "accepted",
                "ato_receipt_id": "B1",
                "ato_timestamp": "2026-05-02T12:00:00Z",
                "warnings": [],
            },
        )
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    await svc.lodge_bas(envelope=b"<BAS/>", period_id="2026-Q3", metadata={})
    assert route.called
    import json

    payload = json.loads(route.calls.last.request.read())
    assert payload["payevent_id"] == "2026-Q3"
