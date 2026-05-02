"""501 stub-mode response → LodgementStatus.STUB."""
from __future__ import annotations

import httpx
import respx

from saebooks.services.lodgement import (
    LodgementStatus,
    RemoteLodgementService,
)
from tests.services.lodgement.conftest import TEST_BASE_URL


@respx.mock
async def test_stp_stub_response_maps_to_stub_status() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            501,
            json={
                "status": "stub",
                "would_have_lodged": True,
                "stub_receipt_id": "stub_abc-123",
                "comment": "lodge-server is stubbed.",
            },
        )
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    result = await svc.lodge_stp(
        envelope=b"<STP>...</STP>",
        payevent_id="payevent-1",
        metadata={"pay_period_end": "2026-04-30"},
    )

    assert result.status is LodgementStatus.STUB
    assert result.ato_receipt_id == "stub_abc-123"
    assert result.ato_timestamp is None
    assert result.warnings == []
    assert result.raw_response["would_have_lodged"] is True


@respx.mock
async def test_bas_stub_response_also_maps_to_stub() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/bas/lodge").mock(
        return_value=httpx.Response(
            501,
            json={
                "status": "stub",
                "would_have_lodged": True,
                "stub_receipt_id": "stub_bas-q3",
                "comment": "stubbed",
            },
        )
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    result = await svc.lodge_bas(
        envelope=b"<BAS>...</BAS>",
        period_id="2026-Q3",
        metadata={"quarter": "Q3"},
    )
    assert result.status is LodgementStatus.STUB
    assert result.ato_receipt_id == "stub_bas-q3"
