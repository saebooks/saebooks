"""RemoteLodgementService.lodge_stp_bundle — the PAYEVNT.0004 parts-array path.

A payroll event is a document SET (parent PAYEVNT + one PAYEVNTEMP per payee),
so it ships to the lodge-server as ``parts`` rather than a single
``envelope_xml``. This test proves the engine emits the N+1 parts in order and
that ``envelope_hash`` equals the sha256 the lodge-server's ``_verify_parts_hash``
recomputes from the decoded parts — so an ordering or encoding drift surfaces as
a hash mismatch, never a silently wrong lodgement.
"""
from __future__ import annotations

import base64
import hashlib

import httpx
import respx

from saebooks.services.lodgement import (
    LodgementStatus,
    RemoteLodgementService,
)
from tests.services.lodgement.conftest import TEST_BASE_URL


def _server_side_parts_hash(parts_b64: list[str]) -> str:
    """Replicate the lodge-server ``_verify_parts_hash``: sha256 over the
    concatenated *decoded* parts, in order. Deliberately NOT reusing the
    engine's expression, so a drift on either side fails this test."""
    h = hashlib.sha256()
    for b64 in parts_b64:
        h.update(base64.b64decode(b64))
    return h.hexdigest()


@respx.mock
async def test_bundle_posts_n_plus_one_parts_with_concatenated_hash() -> None:
    route = respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "accepted",
                "ato_receipt_id": "ATO-RECEIPT-BUNDLE",
                "ato_timestamp": "2026-05-02T12:00:00Z",
                "warnings": [],
            },
        )
    )

    parent = b"<tns:PAYEVNT>employer</tns:PAYEVNT>"
    payee1 = b"<tns:PAYEVNTEMP>payee-1</tns:PAYEVNTEMP>"
    payee2 = b"<tns:PAYEVNTEMP>payee-2</tns:PAYEVNTEMP>"
    parts = [parent, payee1, payee2]

    svc = RemoteLodgementService(submitter_abn="12345678901")
    result = await svc.lodge_stp_bundle(
        parts=parts,
        payevent_id="payevent-bundle-1",
        metadata={"submission_id": "sub-1"},
    )

    assert result.status is LodgementStatus.ACCEPTED
    assert result.ato_receipt_id == "ATO-RECEIPT-BUNDLE"

    body = route.calls.last.request.read()
    import json

    sent = json.loads(body)
    # N+1 parts, no single-envelope key.
    assert "envelope_xml" not in sent
    assert len(sent["parts"]) == 3
    # Order preserved: parent first, then each payee, base64 of the raw bytes.
    assert [base64.b64decode(p) for p in sent["parts"]] == parts
    # The hash is the server-recomputed sha256 over concatenated decoded parts…
    assert sent["envelope_hash"] == _server_side_parts_hash(sent["parts"])
    # …which is exactly sha256(parent + payee1 + payee2).
    assert sent["envelope_hash"] == hashlib.sha256(
        parent + payee1 + payee2
    ).hexdigest()
    assert sent["payevent_id"] == "payevent-bundle-1"
    assert sent["submitter_abn"] == "12345678901"


@respx.mock
async def test_single_payee_bundle_emits_two_parts() -> None:
    route = respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            202,
            json={"status": "queued", "ato_receipt_id": None,
                  "ato_timestamp": None, "warnings": []},
        )
    )
    parent = b"<PAYEVNT/>"
    payee = b"<PAYEVNTEMP/>"

    svc = RemoteLodgementService(submitter_abn="98765432109")
    result = await svc.lodge_stp_bundle(
        parts=[parent, payee], payevent_id="pe-2", metadata={},
    )

    assert result.status is LodgementStatus.QUEUED
    import json

    sent = json.loads(route.calls.last.request.read())
    assert len(sent["parts"]) == 2  # N+1 for a single payee
    assert sent["envelope_hash"] == hashlib.sha256(parent + payee).hexdigest()
