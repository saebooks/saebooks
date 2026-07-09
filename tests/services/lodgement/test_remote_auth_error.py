"""401 → LodgementAuthError; missing-token short-circuit also raises it."""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.services.licence import LicenseService
from saebooks.services.lodgement import (
    LodgementAuthError,
    RemoteLodgementService,
)
from tests.services.lodgement.conftest import TEST_BASE_URL


@respx.mock
async def test_401_response_raises_auth_error() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/stp/lodge").mock(
        return_value=httpx.Response(
            401, json={"detail": "Licence token expired"}
        )
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    with pytest.raises(LodgementAuthError, match="expired"):
        await svc.lodge_stp(
            envelope=b"<STP/>",
            payevent_id="p1",
            metadata={},
        )


async def test_missing_token_raises_auth_error_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cached token → fail loudly without calling the server.

    Override the autouse stub fixture by re-patching to return None.
    """
    monkeypatch.setattr(
        LicenseService, "current_token", classmethod(lambda cls: None)
    )
    svc = RemoteLodgementService(submitter_abn="12345678901")
    with pytest.raises(LodgementAuthError, match="No licence token"):
        await svc.lodge_stp(
            envelope=b"<STP/>",
            payevent_id="p1",
            metadata={},
        )
