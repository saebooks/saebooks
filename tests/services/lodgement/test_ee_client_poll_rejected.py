"""EELodgementClient.poll — rejection path raises EEFilingRejected (offline)."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from saebooks.services.lodgement.adapters.ee_client import EELodgementClient
from saebooks.services.lodgement.exceptions import EEFilingRejected

_FIX = Path(__file__).parent.parent.parent / "fixtures" / "emta_schemas"


def _rejected_xml() -> bytes:
    return (_FIX / "operation_rejected_sample.xml").read_bytes()


def _client(handler) -> EELodgementClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return EELodgementClient(client=http, base_url="https://xroad.test")


async def test_poll_rejected_raises_with_error_lists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_rejected_xml())

    with pytest.raises(EEFilingRejected) as ei:
        await _client(handler).poll("c99bbd83-28f8-48a8-ad2e-02fcad97804f")

    err = ei.value
    assert err.request_id == "c99bbd83-28f8-48a8-ad2e-02fcad97804f"
    assert len(err.xml_errors) == 1
    assert "not expected" in err.xml_errors[0]
    assert len(err.functional_errors) == 1
    assert err.functional_errors[0].error_pointer == "KMD_4"


async def test_poll_unknown_uuid_404_is_upstream_not_rejection() -> None:
    from saebooks.services.lodgement.exceptions import EEUpstreamUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorCode": "NOT_FOUND"})

    with pytest.raises(EEUpstreamUnavailable, match="unknown feedbackReportId"):
        await _client(handler).poll("does-not-exist")
