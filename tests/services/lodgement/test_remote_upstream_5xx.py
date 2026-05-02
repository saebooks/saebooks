"""5xx and transport errors → LodgementUpstreamUnavailable."""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.services.lodgement import (
    LodgementUpstreamUnavailable,
    RemoteLodgementService,
)
from tests.services.lodgement.conftest import TEST_BASE_URL


@respx.mock
async def test_502_raises_upstream_unavailable() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            502, json={"detail": "ATO SBR endpoint timed out"}
        )
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    with pytest.raises(LodgementUpstreamUnavailable) as excinfo:
        await svc.lodge_stp(
            envelope=b"<STP/>",
            payevent_id="p1",
            metadata={},
        )
    assert excinfo.value.status == 502
    assert "ATO SBR" in excinfo.value.detail


@respx.mock
async def test_504_raises_upstream_unavailable() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(504, text="Gateway Timeout")
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    with pytest.raises(LodgementUpstreamUnavailable) as excinfo:
        await svc.lodge_stp(
            envelope=b"<STP/>",
            payevent_id="p1",
            metadata={},
        )
    assert excinfo.value.status == 504


@respx.mock
async def test_transport_error_raises_upstream_unavailable() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        side_effect=httpx.ConnectError("nodename nor servname provided")
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    with pytest.raises(LodgementUpstreamUnavailable) as excinfo:
        await svc.lodge_stp(
            envelope=b"<STP/>",
            payevent_id="p1",
            metadata={},
        )
    assert excinfo.value.status is None
    assert "transport error" in excinfo.value.detail
