"""EELodgementClient.confirm — gated stub (shape UNVERIFIED), zero network.

The KMD3 confirmation service is marked "täpsem info lisandub" in the X-tee
guide §2. ``confirm`` must raise ``EEConfirmServiceUnverified`` BEFORE resolving
any transport, so it fires regardless of credentials and never hits the network.
"""
from __future__ import annotations

import httpx
import pytest

from saebooks.services.lodgement.adapters.ee_client import EELodgementClient
from saebooks.services.lodgement.exceptions import EEConfirmServiceUnverified


async def test_confirm_raises_unverified_with_injected_transport() -> None:
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        hits["n"] += 1
        return httpx.Response(200)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = EELodgementClient(client=http, base_url="https://xroad.test")

    with pytest.raises(EEConfirmServiceUnverified, match="täpsem info lisandub"):
        await client.confirm("uuid-1")
    # Never touched the transport.
    assert hits["n"] == 0


async def test_confirm_raises_even_without_creds() -> None:
    """No injected client, no mtls — confirm is a SPEC gap, not a creds gap, so
    it raises the unverified error, not EELiveCredentialsMissing."""
    client = EELodgementClient()  # mtls=None, client=None
    with pytest.raises(EEConfirmServiceUnverified):
        await client.confirm("uuid-1")
