"""EELodgementClient.submit — MockTransport, zero network egress.

Covers the VERIFIED submit contract (X-tee guide §3): multipart file upload,
201 → {feedbackReportId, estimatedProcessingEndTime}, 400 → {errorCode,
errorMessage}, 5xx → upstream unavailable.
"""
from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from saebooks.services.lodgement.adapters.ee_client import (
    EEFilingEvent,
    EEFilingState,
    EELodgementClient,
    advance,
)
from saebooks.services.lodgement.exceptions import (
    EEFilingValidationError,
    EEUpstreamUnavailable,
)


def _client(handler) -> EELodgementClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return EELodgementClient(client=http, base_url="https://xroad.test")


async def test_submit_201_returns_receipt_and_uuid() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["content_type"] = request.headers.get("content-type", "")
        captured["xroad_client"] = request.headers.get("X-Road-Client")
        return httpx.Response(
            201,
            json={
                "feedbackReportId": "c99bbd83-28f8-48a8-ad2e-02fcad97804f",
                "estimatedProcessingEndTime": "2027-02-20T15:45:00+03:00",
            },
        )

    receipt = await _client(handler).submit(
        b"<xbrli:xbrl/>", section="EE0203001", idempotency_id="req-1"
    )

    assert receipt.request_id == "c99bbd83-28f8-48a8-ad2e-02fcad97804f"
    assert isinstance(receipt.estimated_end, datetime)
    # The +03:00 offset is parsed (reuses remote._parse_iso).
    assert receipt.estimated_end.utcoffset() is not None
    assert captured["method"] == "POST"
    assert "/submit-data" in captured["url"]
    assert "multipart/form-data" in captured["content_type"]
    assert captured["xroad_client"] is not None

    # State transition the caller records.
    assert advance(EEFilingState.IDLE, EEFilingEvent.SUBMIT) is EEFilingState.SUBMITTED


async def test_submit_201_missing_uuid_is_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"estimatedProcessingEndTime": "2027-02-20T15:45:00+03:00"})

    with pytest.raises(EEUpstreamUnavailable, match="feedbackReportId"):
        await _client(handler).submit(b"<x/>", idempotency_id="req-2")


async def test_submit_400_raises_validation_with_error_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "errorCode": "SINGLE_FILE_REQUIRED",
                "errorMessage": "X-road request message must contain exactly one file.",
            },
        )

    with pytest.raises(EEFilingValidationError) as ei:
        await _client(handler).submit(b"<x/>", idempotency_id="req-3")
    assert ei.value.error_code == "SINGLE_FILE_REQUIRED"
    assert "exactly one file" in ei.value.detail


async def test_submit_5xx_raises_upstream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="security server down")

    with pytest.raises(EEUpstreamUnavailable) as ei:
        await _client(handler).submit(b"<x/>", idempotency_id="req-4")
    assert ei.value.status == 503


async def test_submit_transport_error_raises_upstream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(EEUpstreamUnavailable, match="transport error"):
        await _client(handler).submit(b"<x/>", idempotency_id="req-5")
