"""Contract tests for /api/v1/bank-feeds (W4 / Cat-C).

Covers:

* Auth gate (401 without bearer)
* Edition gate (404 in Community)
* POST /connections — happy path + stub-mode persisted as pending
* GET  /connections — list scoped to tenant, no relay round-trip
* GET  /connections/{id} — 404 on absent / cross-tenant
* DELETE /connections/{id} — happy + stub fall-through + relay 404 fall-through
* POST /sync — happy path + stub returns ``stub: true``
* POST /sync — 422 ``period_locked`` when tenant is locked + no override
* POST /sync — accepts when override_reason given
* Tenant isolation: rows from another tenant are invisible

The router resolves ``RemoteBankFeedsService`` via
``request.app.state.bank_feeds_remote`` first, falling back to a fresh
instance. Tests inject an in-memory fake (no httpx, no respx) so we
exercise the router logic without standing up the relay.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from saebooks.api.v1.auth import current_token
from saebooks.config import settings as app_settings
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.bank_feed_external import (
    BankFeedExternalCred,
    BankFeedExternalCredStatus,
)
from saebooks.models.company import Company
from saebooks.models.journal import PeriodLock
from saebooks.services.bank_feeds.exceptions import (
    FeedsAuthError,
    FeedsEditionError,
    FeedsNotFoundError,
    FeedsStubError,
)


# ---------------------------------------------------------------------- #
# Fakes / fixtures                                                       #
# ---------------------------------------------------------------------- #


class _FakeRemote:
    """In-memory stand-in for ``RemoteBankFeedsService``.

    Every method is configurable per test (replace the attribute with
    a callable returning the desired body, or set ``raises=`` to make
    it raise a specific exception). Default behaviour is the live-mode
    happy path so tests only override the bits they care about.
    """

    def __init__(self) -> None:
        self.create_response: dict[str, Any] | None = {
            "connection_id": "conn_abc",
            "consent_url": "https://upstream.example/consent",
            "status": "pending_consent",
        }
        self.create_raises: Exception | None = None
        self.delete_raises: Exception | None = None
        self.get_response: dict[str, Any] = {"id": "conn_abc"}
        self.list_response: list[dict[str, Any]] = []
        self.sync_response: dict[str, Any] = {
            "connection_id": "conn_abc",
            "transactions": [],
            "next_cursor": "cur_2",
            "has_more": False,
        }
        self.sync_raises: Exception | None = None

        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def create_connection(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_connection", kwargs))
        if self.create_raises is not None:
            raise self.create_raises
        return self.create_response or {}

    async def list_connections(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("list_connections", kwargs))
        return self.list_response

    async def get_connection(self, connection_id: str) -> dict[str, Any]:
        self.calls.append(("get_connection", {"connection_id": connection_id}))
        return self.get_response

    async def delete_connection(
        self, connection_id: str, *, idempotency_key: str | None = None
    ) -> None:
        self.calls.append(
            ("delete_connection", {"connection_id": connection_id})
        )
        if self.delete_raises is not None:
            raise self.delete_raises

    async def sync_transactions(
        self, *, connection_id: Any, since_cursor: Any, idempotency_key: str
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "sync_transactions",
                {
                    "connection_id": connection_id,
                    "since_cursor": since_cursor,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        if self.sync_raises is not None:
            raise self.sync_raises
        return self.sync_response


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip the edition so ``FLAG_BANK_FEEDS`` is enabled."""
    monkeypatch.setattr(app_settings, "edition", "enterprise")


@pytest.fixture
def fake_remote() -> _FakeRemote:
    return _FakeRemote()


@pytest.fixture
async def api_client(
    enterprise: None, fake_remote: _FakeRemote
) -> AsyncClient:
    """Bearer-authed client with the fake remote injected on app.state."""
    token = current_token()
    app.state.bank_feeds_remote = fake_remote
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as ac:
            yield ac
    finally:
        if hasattr(app.state, "bank_feeds_remote"):
            delattr(app.state, "bank_feeds_remote")


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _clean_table() -> None:
    """Truncate ``bank_feed_external_creds`` between tests."""
    async with AsyncSessionLocal() as session:
        await session.execute(delete(BankFeedExternalCred))
        await session.commit()
    yield
    async with AsyncSessionLocal() as session:
        await session.execute(delete(BankFeedExternalCred))
        await session.commit()


# ---------------------------------------------------------------------- #
# Auth + edition gates                                                   #
# ---------------------------------------------------------------------- #


async def test_unauthed_request_is_401(
    enterprise: None, unauth_client: AsyncClient
) -> None:
    r = await unauth_client.get("/api/v1/bank-feeds/connections")
    assert r.status_code == 401


async def test_community_build_returns_404(
    monkeypatch: pytest.MonkeyPatch, unauth_client: AsyncClient
) -> None:
    monkeypatch.setattr(app_settings, "edition", "community")
    token = current_token()
    r = await unauth_client.get(
        "/api/v1/bank-feeds/connections",
        headers={"Authorization": f"Bearer {token}"},
    )
    # require_feature short-circuits to 404 before we even get to the
    # bearer check ordering — both 401 and 404 are acceptable per the
    # contract; we expect 404 because community shouldn't advertise the
    # surface at all.
    assert r.status_code == 404


# ---------------------------------------------------------------------- #
# POST /connections                                                      #
# ---------------------------------------------------------------------- #


async def test_create_connection_happy_path(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    r = await api_client.post(
        "/api/v1/bank-feeds/connections",
        json={"bank": "AU000001", "account_label": "Operating"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["consent_url"].startswith("https://upstream.example/")
    assert body["status"] == "pending_consent"
    # Local row should exist.
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(select(BankFeedExternalCred))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].siss_client_id == "conn_abc"


async def test_create_connection_stub_mode_persists_pending_row(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    """501 from relay → router persists a placeholder row, no error."""
    fake_remote.create_raises = FeedsStubError(
        body={
            "stub_connection_id": "stub_conn_xyz",
            "stub_consent_url": "about:stub",
        }
    )
    r = await api_client.post(
        "/api/v1/bank-feeds/connections",
        json={"bank": "AU000001", "account_label": "Operating"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending_consent"
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(BankFeedExternalCred))
        ).scalar_one()
        assert row.siss_client_id == "stub_conn_xyz"


async def test_create_connection_403_maps_to_403(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    fake_remote.create_raises = FeedsEditionError("feeds_enabled is false")
    r = await api_client.post(
        "/api/v1/bank-feeds/connections",
        json={"bank": "X", "account_label": "x"},
    )
    assert r.status_code == 403


async def test_create_connection_401_maps_to_401(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    fake_remote.create_raises = FeedsAuthError("bad token")
    r = await api_client.post(
        "/api/v1/bank-feeds/connections",
        json={"bank": "X", "account_label": "x"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------- #
# GET /connections                                                       #
# ---------------------------------------------------------------------- #


async def test_list_connections_returns_local_rows_only(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    """List is local-only — we never call the relay's /connections."""
    # Seed two rows for the test tenant.
    async with AsyncSessionLocal() as session:
        from saebooks.api.v1.auth import resolve_tenant_id

        # We don't have a request handle here; resolve_tenant_id() with
        # no arg returns the dev-default tenant in the test env, which
        # matches what the bearer flow produces.
        tenant_id = resolve_tenant_id(None)
        for sid in ("conn_a", "conn_b"):
            session.add(
                BankFeedExternalCred(
                    tenant_id=tenant_id,
                    siss_client_id=sid,
                    status=BankFeedExternalCredStatus.PENDING_CONSENT.value,
                )
            )
        await session.commit()

    r = await api_client.get("/api/v1/bank-feeds/connections")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 2
    # The relay should not have been touched.
    assert all(c[0] != "list_connections" for c in fake_remote.calls)


async def test_get_connection_404_for_other_tenant(
    api_client: AsyncClient,
) -> None:
    """A row owned by another tenant returns 404, not 200."""
    other_tenant = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        row = BankFeedExternalCred(
            tenant_id=other_tenant,
            siss_client_id="conn_other",
            status=BankFeedExternalCredStatus.ACTIVE.value,
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    r = await api_client.get(f"/api/v1/bank-feeds/connections/{row_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------- #
# DELETE /connections/{id}                                               #
# ---------------------------------------------------------------------- #


async def test_delete_connection_marks_revoked(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    async with AsyncSessionLocal() as session:
        from saebooks.api.v1.auth import resolve_tenant_id

        tenant_id = resolve_tenant_id(None)
        row = BankFeedExternalCred(
            tenant_id=tenant_id,
            siss_client_id="conn_to_delete",
            status=BankFeedExternalCredStatus.ACTIVE.value,
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    r = await api_client.delete(f"/api/v1/bank-feeds/connections/{row_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "revoked"
    assert body["stub"] is False


async def test_delete_connection_stub_mode_still_revokes_locally(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    fake_remote.delete_raises = FeedsStubError(body={})
    async with AsyncSessionLocal() as session:
        from saebooks.api.v1.auth import resolve_tenant_id

        tenant_id = resolve_tenant_id(None)
        row = BankFeedExternalCred(
            tenant_id=tenant_id,
            siss_client_id="conn_stub_del",
            status=BankFeedExternalCredStatus.ACTIVE.value,
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    r = await api_client.delete(f"/api/v1/bank-feeds/connections/{row_id}")
    assert r.status_code == 200
    assert r.json()["stub"] is True


async def test_delete_connection_relay_404_falls_through(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    """Relay says "I don't know that connection" → still revoke locally."""
    fake_remote.delete_raises = FeedsNotFoundError()
    async with AsyncSessionLocal() as session:
        from saebooks.api.v1.auth import resolve_tenant_id

        tenant_id = resolve_tenant_id(None)
        row = BankFeedExternalCred(
            tenant_id=tenant_id,
            siss_client_id="conn_404_del",
            status=BankFeedExternalCredStatus.ACTIVE.value,
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    r = await api_client.delete(f"/api/v1/bank-feeds/connections/{row_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"


# ---------------------------------------------------------------------- #
# POST /sync                                                             #
# ---------------------------------------------------------------------- #


async def test_sync_happy_path_returns_cursor(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    r = await api_client.post(
        "/api/v1/bank-feeds/sync",
        json={"connection_id": "conn_abc"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_cursor"] == "cur_2"
    assert body["stub"] is False
    assert body["inserted"] == 0  # _find_bank_feed_account_for_connection → None


async def test_sync_stub_mode_returns_stub_flag(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    fake_remote.sync_raises = FeedsStubError(body={"stub_cursor": "x"})
    r = await api_client.post("/api/v1/bank-feeds/sync", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["stub"] is True
    assert body["inserted"] == 0


async def test_sync_blocked_when_period_is_locked(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    """Tenant has a future-dated period lock → 422 ``period_locked``."""
    async with AsyncSessionLocal() as session:
        from saebooks.api.v1.auth import resolve_tenant_id

        tenant_id = resolve_tenant_id(None)
        # Pick the seed company for this tenant.
        company = (
            await session.execute(
                select(Company).where(Company.tenant_id == tenant_id).limit(1)
            )
        ).scalar_one_or_none()
        assert company is not None, "expected a seeded company in the test tenant"
        future = date.today() + timedelta(days=30)
        session.add(
            PeriodLock(
                company_id=company.id,
                locked_through=future,
                reason="test lock",
            )
        )
        await session.commit()

    try:
        r = await api_client.post("/api/v1/bank-feeds/sync", json={})
        assert r.status_code == 422
        body = r.json()
        # FastAPI wraps the dict under ``detail``.
        detail = body.get("detail", body)
        assert detail.get("code") == "period_locked"
    finally:
        # Delete only the lock we added — wiping all PeriodLock rows would
        # destroy the seed-fixture's Q1 2026 lock that other test files
        # (e.g. test_pay_run_v1) rely on.
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(PeriodLock).where(PeriodLock.reason == "test lock")
            )
            await session.commit()


async def test_sync_override_reason_bypasses_period_lock(
    api_client: AsyncClient, fake_remote: _FakeRemote
) -> None:
    async with AsyncSessionLocal() as session:
        from saebooks.api.v1.auth import resolve_tenant_id

        tenant_id = resolve_tenant_id(None)
        company = (
            await session.execute(
                select(Company).where(Company.tenant_id == tenant_id).limit(1)
            )
        ).scalar_one()
        future = date.today() + timedelta(days=30)
        session.add(
            PeriodLock(
                company_id=company.id,
                locked_through=future,
                reason="test lock",
            )
        )
        await session.commit()

    try:
        r = await api_client.post(
            "/api/v1/bank-feeds/sync",
            json={"override_reason": "ATO request"},
        )
        assert r.status_code == 200, r.text
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(PeriodLock).where(PeriodLock.reason == "test lock")
            )
            await session.commit()
