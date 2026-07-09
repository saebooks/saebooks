"""Contract tests for ``/api/v1/inbox/email-addresses`` (phase 3).

Dual gating: the router-wide FLAG_DOCUMENT_INBOX gate plus the
route-level FLAG_INBOX_EMAIL (Business+) — Offline has the inbox but
NOT email-in, so these routes 404 there. Addresses are pure DB: no
vault gate (mint works with the vault disabled).
"""
from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.config import settings as _settings
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company

pytestmark = pytest.mark.postgres_only

_TOKEN_RE = re.compile(r"^[a-z2-7]{16}$")


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {current_token()}"}


@pytest.fixture
async def business_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setattr(_settings, "edition", "business")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=_bearer()
    ) as ac:
        yield ac


@pytest.fixture
async def offline_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """Offline has FLAG_DOCUMENT_INBOX but NOT FLAG_INBOX_EMAIL."""
    monkeypatch.setattr(_settings, "edition", "offline")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=_bearer()
    ) as ac:
        yield ac


@pytest.fixture
async def default_company_id() -> str:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(
                    Company.tenant_id == DEFAULT_TENANT_ID,
                    Company.archived_at.is_(None),
                )
                .limit(1)
            )
        ).scalars().first()
        assert company is not None
        return str(company.id)


async def _mint(client: AsyncClient, **body: Any) -> Any:
    return await client.post("/api/v1/inbox/email-addresses", json=body)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


async def test_requires_bearer() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/api/v1/inbox/email-addresses")
    assert r.status_code == 401


async def test_offline_404s_email_but_keeps_inbox(
    offline_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FLAG_INBOX_EMAIL is Business+ — the address routes vanish below
    it while the rest of the inbox stays (dual-gate contract)."""
    r = await offline_client.get("/api/v1/inbox/email-addresses")
    assert r.status_code == 404
    r = await _mint(offline_client)
    assert r.status_code == 404
    # The sibling supplier-rules route (FLAG_DOCUMENT_INBOX only) works.
    r = await offline_client.get("/api/v1/inbox/supplier-rules")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------------


async def test_mint_returns_token_and_no_address_without_domain(
    business_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_settings, "inbox_mail_domain", "")
    r = await _mint(business_client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert _TOKEN_RE.match(body["token"]), body["token"]
    assert body["address"] is None  # domain not configured yet — pure env wiring
    assert body["active"] is True
    assert body["company_id"] is None
    assert body["revoked_at"] is None


async def test_mint_address_composed_from_configured_domain(
    business_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_settings, "inbox_mail_domain", "in.saebooks.test")
    r = await _mint(business_client)
    body = r.json()
    assert body["address"] == f"{body['token']}@in.saebooks.test"


async def test_mint_multiple_active_addresses_per_tenant(
    business_client: AsyncClient, default_company_id: str
) -> None:
    """Multi-entity tenants mint one address per company — several
    ACTIVE at once is the design, not a conflict."""
    r1 = await _mint(business_client)
    r2 = await _mint(business_client, company_id=default_company_id)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["token"] != r2.json()["token"]
    assert r2.json()["company_id"] == default_company_id

    r = await business_client.get("/api/v1/inbox/email-addresses")
    tokens = {a["token"] for a in r.json()["items"]}
    assert {r1.json()["token"], r2.json()["token"]} <= tokens


async def test_mint_foreign_company_404(business_client: AsyncClient) -> None:
    r = await _mint(business_client, company_id=str(uuid.uuid4()))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


async def test_revoke_is_soft_and_idempotent(
    business_client: AsyncClient,
) -> None:
    addr = (await _mint(business_client)).json()
    r = await business_client.post(
        f"/api/v1/inbox/email-addresses/{addr['id']}/revoke"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active"] is False
    assert body["revoked_at"] is not None

    # Idempotent — a second revoke succeeds and keeps the original stamp.
    r2 = await business_client.post(
        f"/api/v1/inbox/email-addresses/{addr['id']}/revoke"
    )
    assert r2.status_code == 200
    assert r2.json()["revoked_at"] == body["revoked_at"]

    # Hidden from the default list, visible with include_revoked.
    r = await business_client.get("/api/v1/inbox/email-addresses")
    assert addr["id"] not in [a["id"] for a in r.json()["items"]]
    r = await business_client.get(
        "/api/v1/inbox/email-addresses", params={"include_revoked": True}
    )
    assert addr["id"] in [a["id"] for a in r.json()["items"]]


async def test_revoke_unknown_id_404(business_client: AsyncClient) -> None:
    r = await business_client.post(
        f"/api/v1/inbox/email-addresses/{uuid.uuid4()}/revoke"
    )
    assert r.status_code == 404
