"""200 ACCEPTED response → status, receipt, timestamp parsed."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import respx

from saebooks.services.lodgement import (
    LodgementStatus,
    RemoteLodgementService,
)
from tests.services.lodgement.conftest import TEST_BASE_URL


@respx.mock
async def test_accepted_parses_receipt_and_timestamp() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "accepted",
                "ato_receipt_id": "ATO-RECEIPT-XYZ",
                "ato_timestamp": "2026-05-02T12:00:00Z",
                "warnings": ["payee count exceeded 1000"],
            },
        )
    )

    svc = RemoteLodgementService(submitter_abn="12345678901")
    result = await svc.lodge_stp(
        envelope=b"<STP>payload</STP>",
        payevent_id="payevent-1",
        metadata={"employee_count": 7},
    )

    assert result.status is LodgementStatus.ACCEPTED
    assert result.ato_receipt_id == "ATO-RECEIPT-XYZ"
    assert result.ato_timestamp == datetime(
        2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc
    )
    assert result.warnings == ["payee count exceeded 1000"]


@respx.mock
async def test_queued_202_maps_to_queued_status() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            202,
            json={
                "status": "queued",
                "ato_receipt_id": None,
                "ato_timestamp": None,
                "warnings": [],
            },
        )
    )

    svc = RemoteLodgementService(submitter_abn="12345678901")
    result = await svc.lodge_stp(
        envelope=b"<STP/>",
        payevent_id="payevent-2",
        metadata={},
    )
    assert result.status is LodgementStatus.QUEUED
    assert result.ato_receipt_id is None
