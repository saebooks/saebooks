"""The loud live gate — no X-Road creds → EELiveCredentialsMissing, ZERO network.

The single most important guarantee of Module 3: with no injected transport and
no complete MtlsConfig, every network-needing EE call fails loud BEFORE opening a
socket. These tests pass ``mtls=None, client=None`` EXPLICITLY (advisor point 1)
rather than relying on a clean env, and assert that no ``httpx.AsyncClient`` is
ever constructed.
"""
from __future__ import annotations

import httpx
import pytest

from saebooks.services.lodgement import get_adapter
from saebooks.services.lodgement.adapters import ee_client as ee_client_mod
from saebooks.services.lodgement.adapters.ee import EELodgementAdapter
from saebooks.services.lodgement.adapters.ee_client import (
    EELodgementClient,
    MtlsConfig,
)
from saebooks.services.lodgement.exceptions import EELiveCredentialsMissing


@pytest.fixture
def _no_real_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Trip-wire: fail if a real transport is ever constructed."""
    built = {"n": 0}

    def _boom(*args: object, **kwargs: object) -> httpx.AsyncClient:
        built["n"] += 1
        raise AssertionError("a real httpx.AsyncClient was constructed — live gate leaked!")

    monkeypatch.setattr(ee_client_mod.httpx, "AsyncClient", _boom)
    return built


async def test_client_submit_no_creds_fails_loud_zero_network(
    _no_real_client: dict[str, int],
) -> None:
    client = EELodgementClient(mtls=None, client=None)
    with pytest.raises(EELiveCredentialsMissing):
        await client.submit(b"<x/>", idempotency_id="req-1")
    assert _no_real_client["n"] == 0


async def test_client_poll_no_creds_fails_loud_zero_network(
    _no_real_client: dict[str, int],
) -> None:
    client = EELodgementClient(mtls=None, client=None)
    with pytest.raises(EELiveCredentialsMissing):
        await client.poll("uuid-1")
    assert _no_real_client["n"] == 0


async def test_partial_mtls_config_still_gated(
    _no_real_client: dict[str, int],
) -> None:
    """A config missing any field is incomplete → still the loud gate."""
    partial = MtlsConfig(
        client_cert_path="/x/cert.pem",
        client_key_path="/x/key.pem",
        security_server_url="https://ss.local",
        xroad_client_header=None,  # <- missing
    )
    assert partial.is_complete() is False
    client = EELodgementClient(mtls=partial, client=None)
    with pytest.raises(EELiveCredentialsMissing):
        await client.submit(b"<x/>", idempotency_id="req-1")
    assert _no_real_client["n"] == 0


def test_complete_mtls_config_is_complete() -> None:
    cfg = MtlsConfig(
        client_cert_path="/x/cert.pem",
        client_key_path="/x/key.pem",
        security_server_url="https://ss.local",
        xroad_client_header="ee-test/COM/10123456/kmd3",
    )
    assert cfg.is_complete() is True


async def test_default_adapter_lodge_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
    _no_real_client: dict[str, int],
) -> None:
    """Default-constructed EE adapter (no env creds) → loud gate on lodge."""
    for var in (
        "EE_XROAD_CLIENT_CERT",
        "EE_XROAD_CLIENT_KEY",
        "EE_XROAD_SECURITY_SERVER",
        "EE_XROAD_CLIENT_HEADER",
    ):
        monkeypatch.delenv(var, raising=False)

    adapter = get_adapter("EE")
    assert isinstance(adapter, EELodgementAdapter)
    with pytest.raises(EELiveCredentialsMissing):
        await adapter.lodge("vat_kmd", b"<x/>", "id-1", {})
    assert _no_real_client["n"] == 0


async def test_adapter_with_injected_mock_transport_works() -> None:
    """The gate is about CREDS, not a hard block: an injected MockTransport
    client drives the whole path offline, proving the adapter delegates."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "feedbackReportId": "uuid-adapter",
                "estimatedProcessingEndTime": "2027-02-20T15:45:00+03:00",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    injected = EELodgementClient(client=http, base_url="https://xroad.test")
    adapter = EELodgementAdapter(client=injected)

    receipt = await adapter.lodge("vat_kmd", b"<xbrli:xbrl/>", "id-1", {})
    assert receipt.request_id == "uuid-adapter"


async def test_adapter_unknown_route_raises() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(201)))
    adapter = EELodgementAdapter(client=EELodgementClient(client=http, base_url="https://x.test"))
    with pytest.raises(ValueError, match="does not support lodge route"):
        await adapter.lodge("bogus", b"<x/>", "id-1", {})
