"""Unit tests for ``RemoteBankFeedsService`` (W4 / Cat-C).

Mirror of ``tests/services/lodgement/test_remote_*.py`` — one assertion
per status-code mapping, plus a happy-path test per public method.
respx mocks the relay; the licence token is stubbed via the
``tests/services/bank_feeds/conftest.py`` autouse fixtures.

These tests live one directory up from the legacy ``tests/services/
bank_feeds/`` suite (which targets the SISS-direct stack and has no
overlap with the relay client). Pytest still picks the conftest up via
the ``bank_feeds`` subdirectory because we import the constants from
there explicitly; the env-pinning fixture has to be re-declared at
module level for tests in this file. We keep the autouse fixtures in
the subdirectory's conftest because that's where the existing remote
client tests sit, and apply local equivalents here for portability.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from saebooks.services.bank_feeds import (
    FeedsAuthError,
    FeedsEditionError,
    FeedsIdempotencyConflict,
    FeedsNotFoundError,
    FeedsStubError,
    FeedsUpstreamError,
    FeedsUpstreamUnavailable,
    FeedsValidationError,
    RemoteBankFeedsService,
)
from saebooks.services.licence import LicenseService


TEST_BASE_URL = "https://feeds.test"
TEST_TOKEN = "test.licence.token"


@pytest.fixture(autouse=True)
def _stub_licence_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        LicenseService, "current_token", classmethod(lambda cls: TEST_TOKEN)
    )


@pytest.fixture(autouse=True)
def _pin_feeds_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEEDS_SERVER_URL", TEST_BASE_URL)


# ---------------------------------------------------------------------- #
# create_connection                                                      #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_create_connection_201_returns_connection_id_and_url() -> None:
    route = respx.post(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(
            201,
            json={
                "connection_id": "conn_abc123",
                "consent_url": "https://upstream.example/consent/abc",
                "status": "pending_consent",
            },
        )
    )
    svc = RemoteBankFeedsService()
    body = await svc.create_connection(
        bank="AU000001",
        account_label="Sauer — Operating",
        idempotency_key="key-1",
    )
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert sent.headers["Idempotency-Key"] == "key-1"
    assert body["connection_id"] == "conn_abc123"
    assert body["consent_url"].startswith("https://upstream.example/")


@respx.mock
async def test_create_connection_501_stub_raises_FeedsStubError_with_body() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(
            501,
            json={
                "status": "stub",
                "would_have_created": True,
                "stub_connection_id": "stub_conn_xyz",
                "stub_consent_url": "about:stub",
                "comment": "feeds-server is stubbed.",
            },
        )
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsStubError) as ei:
        await svc.create_connection(
            bank="AU000001",
            account_label="x",
            idempotency_key="k",
        )
    assert ei.value.body["stub_connection_id"] == "stub_conn_xyz"


@respx.mock
async def test_create_connection_403_raises_FeedsEditionError() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(
            403,
            json={"detail": "feeds_enabled is false"},
        )
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsEditionError):
        await svc.create_connection(
            bank="X",
            account_label="x",
            idempotency_key="k",
        )


@respx.mock
async def test_create_connection_401_raises_FeedsAuthError() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(401, json={"detail": "bad token"})
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsAuthError):
        await svc.create_connection(
            bank="X",
            account_label="x",
            idempotency_key="k",
        )


@respx.mock
async def test_create_connection_409_carries_request_hashes() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(
            409,
            json={
                "detail": "idempotency conflict",
                "first_request_hash": "deadbeef",
                "this_request_hash": "cafebabe",
            },
        )
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsIdempotencyConflict) as ei:
        await svc.create_connection(
            bank="X",
            account_label="x",
            idempotency_key="k",
        )
    assert ei.value.first_request_hash == "deadbeef"
    assert ei.value.this_request_hash == "cafebabe"


@respx.mock
async def test_create_connection_400_raises_FeedsValidationError() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(400, json={"detail": "bad institution_id"})
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsValidationError):
        await svc.create_connection(
            bank="???",
            account_label="x",
            idempotency_key="k",
        )


@respx.mock
async def test_create_connection_502_raises_FeedsUpstreamError() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(502, json={"detail": "siss returned 5xx"})
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsUpstreamError):
        await svc.create_connection(
            bank="X",
            account_label="x",
            idempotency_key="k",
        )


@respx.mock
async def test_create_connection_503_raises_FeedsUpstreamUnavailable() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(503, json={"detail": "siss unreachable"})
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsUpstreamUnavailable):
        await svc.create_connection(
            bank="X",
            account_label="x",
            idempotency_key="k",
        )


# ---------------------------------------------------------------------- #
# list_connections                                                       #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_list_connections_unwraps_rows() -> None:
    respx.get(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(
            200,
            json={
                "license_id": "lic_xyz",
                "rows": [
                    {"id": "conn_1", "status": "active"},
                    {"id": "conn_2", "status": "revoked"},
                ],
            },
        )
    )
    svc = RemoteBankFeedsService()
    rows = await svc.list_connections()
    assert len(rows) == 2
    assert rows[0]["id"] == "conn_1"


@respx.mock
async def test_list_connections_empty_response_returns_empty_list() -> None:
    respx.get(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(200, json={"rows": []})
    )
    svc = RemoteBankFeedsService()
    rows = await svc.list_connections()
    assert rows == []


# ---------------------------------------------------------------------- #
# get_connection                                                         #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_get_connection_404_raises_FeedsNotFoundError() -> None:
    respx.get(f"{TEST_BASE_URL}/api/v1/connections/conn_x").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsNotFoundError):
        await svc.get_connection("conn_x")


@respx.mock
async def test_get_connection_200_happy_path() -> None:
    respx.get(f"{TEST_BASE_URL}/api/v1/connections/conn_y").mock(
        return_value=httpx.Response(
            200,
            json={"id": "conn_y", "status": "active"},
        )
    )
    svc = RemoteBankFeedsService()
    body = await svc.get_connection("conn_y")
    assert body["status"] == "active"


# ---------------------------------------------------------------------- #
# delete_connection                                                      #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_delete_connection_200_returns_none() -> None:
    respx.delete(f"{TEST_BASE_URL}/api/v1/connections/conn_z").mock(
        return_value=httpx.Response(
            200,
            json={"status": "revoked", "revoked_at": "2026-05-04T00:00:00Z"},
        )
    )
    svc = RemoteBankFeedsService()
    out = await svc.delete_connection("conn_z")
    assert out is None


@respx.mock
async def test_delete_connection_501_stub_raises_FeedsStubError() -> None:
    respx.delete(f"{TEST_BASE_URL}/api/v1/connections/conn_w").mock(
        return_value=httpx.Response(
            501,
            json={"status": "stub", "would_have_revoked": True},
        )
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsStubError):
        await svc.delete_connection("conn_w")


# ---------------------------------------------------------------------- #
# sync_transactions                                                      #
# ---------------------------------------------------------------------- #


@respx.mock
async def test_sync_transactions_200_returns_body_verbatim() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/transactions/sync").mock(
        return_value=httpx.Response(
            200,
            json={
                "connection_id": "conn_a",
                "transactions": [{"id": "t1"}, {"id": "t2"}],
                "next_cursor": "cur_2",
                "has_more": False,
            },
        )
    )
    svc = RemoteBankFeedsService()
    body = await svc.sync_transactions(
        connection_id="conn_a",
        since_cursor="cur_1",
        idempotency_key="k",
    )
    assert body["next_cursor"] == "cur_2"
    assert len(body["transactions"]) == 2


@respx.mock
async def test_sync_transactions_501_stub_raises_with_body() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/transactions/sync").mock(
        return_value=httpx.Response(
            501,
            json={
                "status": "stub",
                "would_have_synced": True,
                "stub_cursor": "stub_cur_xyz",
                "comment": "stubbed",
            },
        )
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsStubError) as ei:
        await svc.sync_transactions(
            connection_id=None,
            since_cursor=None,
            idempotency_key="k",
        )
    assert ei.value.body.get("stub_cursor") == "stub_cur_xyz"


# ---------------------------------------------------------------------- #
# Transport / token edge cases                                           #
# ---------------------------------------------------------------------- #


async def test_no_licence_token_raises_FeedsAuthError(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``LicenseService.current_token()`` returns ``None`` we don't
    even reach the wire — fail fast with ``FeedsAuthError``.
    """
    monkeypatch.setattr(
        LicenseService, "current_token", classmethod(lambda cls: None)
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsAuthError):
        await svc.create_connection(
            bank="X",
            account_label="x",
            idempotency_key="k",
        )


@respx.mock
async def test_transport_error_raises_FeedsUpstreamUnavailable() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/connections").mock(
        side_effect=httpx.ConnectError("relay down")
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsUpstreamUnavailable):
        await svc.create_connection(
            bank="X",
            account_label="x",
            idempotency_key="k",
        )


@respx.mock
async def test_unknown_5xx_collapses_to_FeedsUpstreamUnavailable() -> None:
    respx.post(f"{TEST_BASE_URL}/api/v1/connections").mock(
        return_value=httpx.Response(500, json={"detail": "bug"})
    )
    svc = RemoteBankFeedsService()
    with pytest.raises(FeedsUpstreamUnavailable):
        await svc.create_connection(
            bank="X",
            account_label="x",
            idempotency_key="k",
        )
