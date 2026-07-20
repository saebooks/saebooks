"""EELodgementClient.poll — pending → accepted paths (MockTransport, offline).

Exercises the full submit → poll(pending) → poll(accepted) sequence across
sequential calls to the same client, using a queued handler (advisor point 6),
and asserts the parsed operationAccepted feedback + the state transitions.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx

from saebooks.services.lodgement.adapters.ee_client import (
    EEFilingEvent,
    EEFilingState,
    EELodgementClient,
    advance,
)

_FIX = Path(__file__).parent.parent.parent / "fixtures" / "emta_schemas"


def _accepted_xml() -> bytes:
    return (_FIX / "operation_accepted_sample.xml").read_bytes()


def _client(handler) -> EELodgementClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return EELodgementClient(client=http, base_url="https://xroad.test")


async def test_poll_202_is_pending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    res = await _client(handler).poll("uuid-1")
    assert res.state is EEFilingState.PENDING
    assert res.feedback is None


async def test_poll_200_empty_body_is_pending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    res = await _client(handler).poll("uuid-1")
    assert res.state is EEFilingState.PENDING
    assert res.feedback is None


async def test_poll_200_accepted_parses_feedback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/return-data/uuid-1" in str(request.url)
        return httpx.Response(200, content=_accepted_xml())

    res = await _client(handler).poll("uuid-1")
    assert res.state is EEFilingState.ACCEPTED
    assert res.feedback is not None
    assert res.feedback.accepted is True
    assert res.feedback.vat_payable == Decimal("1234.56")
    assert res.feedback.declaration_state == "SUBMITTED"


async def test_full_submit_poll_pending_then_accepted_sequence() -> None:
    """One client, sequential calls: submit(201) → poll(202) → poll(accepted).

    Drives the whole state machine offline; a call counter switches the handler
    response so the same URLs return different bodies across calls.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.method == "POST" and "/submit-data" in str(request.url):
            return httpx.Response(
                201,
                json={
                    "feedbackReportId": "uuid-seq",
                    "estimatedProcessingEndTime": "2027-02-20T15:45:00+03:00",
                },
            )
        # First GET → still processing; second GET → accepted.
        if calls["n"] <= 2:
            return httpx.Response(202)
        return httpx.Response(200, content=_accepted_xml())

    client = _client(handler)

    state = EEFilingState.IDLE
    receipt = await client.submit(b"<x/>", idempotency_id="seq")
    state = advance(state, EEFilingEvent.SUBMIT)
    assert state is EEFilingState.SUBMITTED
    assert receipt.request_id == "uuid-seq"

    r1 = await client.poll(receipt.request_id)
    assert r1.state is EEFilingState.PENDING
    state = advance(state, EEFilingEvent.POLL_PENDING)
    assert state is EEFilingState.PENDING

    r2 = await client.poll(receipt.request_id)
    assert r2.state is EEFilingState.ACCEPTED
    state = advance(state, EEFilingEvent.POLL_ACCEPTED)
    assert state is EEFilingState.ACCEPTED
    assert r2.feedback is not None
    assert r2.feedback.vat_payable == Decimal("1234.56")
