"""Status-transition tests for /api/v1/payments/{id}/post.

Covers:
* POST /{id}/post happy path — DRAFT → POSTED, version bumped
* POST /{id}/post on already-POSTED payment → 422
* POST /{id}/post with stale If-Match → 409
* POST /{id}/post without If-Match → 428
* POST /{id}/post on missing payment → 404

Critic finding: #12 — POST /api/v1/payments/{id}/post was missing from REST,
asymmetric with /invoices/{id}/post and /bills/{id}/post.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.contact import Contact


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def payment_deps() -> dict[str, str]:
    """Return IDs needed to build a payment payload."""
    async with AsyncSessionLocal() as session:
        bank = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                    Account.is_header.is_(False),
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()

    assert bank is not None, "Test DB has no ASSET account"
    assert contact is not None, "Test DB has no contact"
    return {
        "bank_account_id": str(bank.id),
        "contact_id": str(contact.id),
    }


def _payment_payload(deps: dict[str, str]) -> dict:
    return {
        "contact_id": deps["contact_id"],
        "bank_account_id": deps["bank_account_id"],
        "payment_date": "2026-04-15",
        "amount": "500.00",
        "direction": "INCOMING",
        "method": "eft",
        "reference": "TRANS-TEST-001",
    }


async def _create_draft(client: AsyncClient, deps: dict[str, str]) -> dict:
    r = await client.post("/api/v1/payments", json=_payment_payload(deps))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# POST /{id}/post — happy path
# ---------------------------------------------------------------------------


async def test_payment_post_happy(
    api_client: AsyncClient, payment_deps: dict[str, str]
) -> None:
    """DRAFT → POSTED: returns 200 with POSTED status and bumped version."""
    body = await _create_draft(api_client, payment_deps)
    payment_id = body["id"]
    version = body["version"]

    r = await api_client.post(
        f"/api/v1/payments/{payment_id}/post",
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    posted = r.json()
    assert posted["status"] == "POSTED"
    assert posted["version"] == version + 1
    assert posted["id"] == payment_id


# ---------------------------------------------------------------------------
# POST /{id}/post — already POSTED → 422
# ---------------------------------------------------------------------------


async def test_payment_post_already_posted_422(
    api_client: AsyncClient, payment_deps: dict[str, str]
) -> None:
    """Double-posting returns 422 with a non-draft error message."""
    body = await _create_draft(api_client, payment_deps)
    payment_id = body["id"]
    version = body["version"]

    r1 = await api_client.post(
        f"/api/v1/payments/{payment_id}/post",
        headers={"If-Match": str(version)},
    )
    assert r1.status_code == 200, r1.text
    new_version = r1.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/payments/{payment_id}/post",
        headers={"If-Match": str(new_version)},
    )
    assert r2.status_code == 422, r2.text


# ---------------------------------------------------------------------------
# POST /{id}/post — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_payment_post_stale_if_match_409(
    api_client: AsyncClient, payment_deps: dict[str, str]
) -> None:
    """Stale version returns 409 with current state in body."""
    body = await _create_draft(api_client, payment_deps)
    payment_id = body["id"]
    version = body["version"]

    r = await api_client.post(
        f"/api/v1/payments/{payment_id}/post",
        headers={"If-Match": str(version + 99)},
    )
    assert r.status_code == 409, r.text
    conflict = r.json()
    assert "current" in conflict


# ---------------------------------------------------------------------------
# POST /{id}/post — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_payment_post_no_if_match_428(
    api_client: AsyncClient, payment_deps: dict[str, str]
) -> None:
    """Missing If-Match header returns 428."""
    body = await _create_draft(api_client, payment_deps)
    payment_id = body["id"]

    r = await api_client.post(f"/api/v1/payments/{payment_id}/post")
    assert r.status_code == 428, r.text


# ---------------------------------------------------------------------------
# POST /{id}/post — missing payment → 404
# ---------------------------------------------------------------------------


async def test_payment_post_missing_404(api_client: AsyncClient) -> None:
    """Non-existent payment ID returns 404."""
    r = await api_client.post(
        f"/api/v1/payments/{uuid.uuid4()}/post",
        headers={"If-Match": "1"},
    )
    assert r.status_code == 404, r.text
