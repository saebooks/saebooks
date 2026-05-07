"""422 → LodgementRejected with ato_errors propagated.

(The contract pins 422 to ATO-side rejections — validation, schema,
TFN issues, etc. The 400 path is for client-side problems and is
covered separately.)
"""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.services.lodgement import (
    LodgementRejected,
    LodgementValidationError,
    RemoteLodgementService,
)
from tests.services.lodgement.conftest import TEST_BASE_URL


@respx.mock
async def test_422_raises_rejected_with_ato_errors() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            422,
            json={
                "detail": "ATO rejected envelope",
                "ato_errors": [
                    {"code": "CMN.ATO.GEN.000001", "message": "Invalid TFN"},
                    {"code": "CMN.ATO.GEN.000002", "message": "Missing BMS ID"},
                ],
            },
        )
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    with pytest.raises(LodgementRejected) as excinfo:
        await svc.lodge_stp(
            envelope=b"<STP/>",
            payevent_id="payevent-422",
            metadata={},
        )
    err = excinfo.value
    assert err.detail == "ATO rejected envelope"
    assert len(err.ato_errors) == 2
    assert err.ato_errors[0]["code"] == "CMN.ATO.GEN.000001"
    assert err.raw_response["ato_errors"][1]["message"] == "Missing BMS ID"


@respx.mock
async def test_400_raises_validation_error() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            400,
            json={"detail": "envelope_hash does not match envelope_xml"},
        )
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    with pytest.raises(LodgementValidationError, match="envelope_hash"):
        await svc.lodge_stp(
            envelope=b"<STP/>",
            payevent_id="p1",
            metadata={},
        )
