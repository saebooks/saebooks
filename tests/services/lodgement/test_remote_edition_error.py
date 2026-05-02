"""403 → LodgementEditionError. Backstop for the route-level gate."""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.services.lodgement import (
    LodgementEditionError,
    RemoteLodgementService,
)
from tests.services.lodgement.conftest import TEST_BASE_URL


@respx.mock
async def test_403_raises_edition_error() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            403,
            json={"detail": "Licence edition 'business' lacks ato_sbr"},
        )
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    with pytest.raises(LodgementEditionError, match="ato_sbr"):
        await svc.lodge_stp(
            envelope=b"<STP/>",
            payevent_id="p1",
            metadata={},
        )
