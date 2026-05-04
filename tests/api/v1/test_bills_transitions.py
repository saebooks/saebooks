"""Status-transition tests for /api/v1/bills/{id}/post and /void.

Covers:
* POST /{id}/post happy path — DRAFT → POSTED, version bumped
* POST /{id}/post on already-POSTED bill → 422
* POST /{id}/post with stale If-Match → 409
* POST /{id}/post with no lines → 422
* POST /{id}/void happy path — POSTED → VOIDED, version bumped
* POST /{id}/void on already-VOIDED bill → 422
* POST /{id}/void with stale If-Match → 409
* Post then void produces ≥2 change_log entries
* Tenant isolation — tenant B cannot post tenant A's bill → 404
"""
from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog
from saebooks.models.contact import Contact


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
async def bill_deps() -> dict[str, str]:
    """Return IDs needed to build a bill payload."""
    async with AsyncSessionLocal() as session:
        expense = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(Contact.archived_at.is_(None), Contact.tenant_id == DEFAULT_TENANT_ID).limit(1)
            )
        ).scalars().first()

    assert expense is not None, "Test DB has no EXPENSE account"
    assert contact is not None, "Test DB has no contact"
    return {
        "expense_account_id": str(expense.id),
        "contact_id": str(contact.id),
    }


def _bill_payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "contact_id": deps["contact_id"],
        "issue_date": "2026-04-01",
        "due_date": "2026-05-01",
        "notes": "Transition test bill",
        "lines": [
            {
                "description": "Office supplies",
                "account_id": deps["expense_account_id"],
                "quantity": "1",
                "unit_price": "200.00",
                "discount_pct": "0",
            },
        ],
    }
    base.update(overrides)
    return base


async def _create_bill(client: AsyncClient, deps: dict[str, str]) -> dict:
    """Helper: create a DRAFT bill and return the response body."""
    r = await client.post("/api/v1/bills", json=_bill_payload(deps))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# POST /{id}/post — happy path
# ---------------------------------------------------------------------------


async def test_bill_post_happy(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """DRAFT → POSTED: returns 200 with POSTED status and bumped version."""
    body = await _create_bill(api_client, bill_deps)
    bill_id = body["id"]
    version = body["version"]

    r = await api_client.post(
        f"/api/v1/bills/{bill_id}/post",
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    posted = r.json()
    assert posted["status"] == "POSTED"
    assert posted["version"] == version + 1
    assert posted["id"] == bill_id


# ---------------------------------------------------------------------------
# POST /{id}/post — already posted → 422
# ---------------------------------------------------------------------------


async def test_bill_post_already_posted_422(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """Posting an already-POSTED bill must return 422."""
    body = await _create_bill(api_client, bill_deps)
    bill_id = body["id"]

    # First post
    r1 = await api_client.post(
        f"/api/v1/bills/{bill_id}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r1.status_code == 200, r1.text
    posted_version = r1.json()["version"]

    # Second post on same bill
    r2 = await api_client.post(
        f"/api/v1/bills/{bill_id}/post",
        headers={"If-Match": str(posted_version)},
    )
    assert r2.status_code == 422


# ---------------------------------------------------------------------------
# POST /{id}/post — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_bill_post_stale_version_409(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """Posting with a stale If-Match header must return 409."""
    body = await _create_bill(api_client, bill_deps)
    bill_id = body["id"]

    r = await api_client.post(
        f"/api/v1/bills/{bill_id}/post",
        headers={"If-Match": "99"},
    )
    assert r.status_code == 409
    conflict = r.json()
    assert conflict["detail"] == "version mismatch"
    assert conflict["current"]["id"] == bill_id


# ---------------------------------------------------------------------------
# POST /{id}/post — no lines → 422
# ---------------------------------------------------------------------------


async def test_bill_post_empty_lines_422(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """Posting a bill with no lines must return 422."""
    payload = _bill_payload(bill_deps)
    payload["lines"] = []
    r = await api_client.post("/api/v1/bills", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    bill_id = body["id"]
    version = body["version"]

    r2 = await api_client.post(
        f"/api/v1/bills/{bill_id}/post",
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 422


# ---------------------------------------------------------------------------
# POST /{id}/void — happy path (from POSTED)
# ---------------------------------------------------------------------------


async def test_bill_void_happy(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """DRAFT → POSTED → VOIDED: void returns 200 with VOIDED status."""
    body = await _create_bill(api_client, bill_deps)
    bill_id = body["id"]

    # Post first
    r1 = await api_client.post(
        f"/api/v1/bills/{bill_id}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r1.status_code == 200, r1.text
    posted_version = r1.json()["version"]

    # Now void
    r2 = await api_client.post(
        f"/api/v1/bills/{bill_id}/void",
        headers={"If-Match": str(posted_version)},
    )
    assert r2.status_code == 200, r2.text
    voided = r2.json()
    assert voided["status"] == "VOIDED"
    assert voided["version"] == posted_version + 1
    assert voided["id"] == bill_id


# ---------------------------------------------------------------------------
# POST /{id}/void — already voided → 422
# ---------------------------------------------------------------------------


async def test_bill_void_already_voided_422(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """Voiding an already-VOIDED bill must return 422."""
    body = await _create_bill(api_client, bill_deps)
    bill_id = body["id"]

    # Post then void
    r1 = await api_client.post(
        f"/api/v1/bills/{bill_id}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r1.status_code == 200, r1.text
    posted_version = r1.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/bills/{bill_id}/void",
        headers={"If-Match": str(posted_version)},
    )
    assert r2.status_code == 200, r2.text
    voided_version = r2.json()["version"]

    # Second void attempt
    r3 = await api_client.post(
        f"/api/v1/bills/{bill_id}/void",
        headers={"If-Match": str(voided_version)},
    )
    assert r3.status_code == 422


# ---------------------------------------------------------------------------
# POST /{id}/void — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_bill_void_stale_409(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """Voiding with a stale If-Match header must return 409."""
    body = await _create_bill(api_client, bill_deps)
    bill_id = body["id"]

    r = await api_client.post(
        f"/api/v1/bills/{bill_id}/void",
        headers={"If-Match": "99"},
    )
    assert r.status_code == 409
    conflict = r.json()
    assert conflict["detail"] == "version mismatch"
    assert conflict["current"]["id"] == bill_id


# ---------------------------------------------------------------------------
# change_log — post + void = ≥ 2 transition entries
# ---------------------------------------------------------------------------


async def test_bill_change_log_has_two_entries(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """Post then void produces change_log rows with ops 'post' and 'void'."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    body = await _create_bill(api_client, bill_deps)
    bill_id = body["id"]

    # Post
    r1 = await api_client.post(
        f"/api/v1/bills/{bill_id}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r1.status_code == 200, r1.text
    posted_version = r1.json()["version"]

    # Void
    r2 = await api_client.post(
        f"/api/v1/bills/{bill_id}/void",
        headers={"If-Match": str(posted_version)},
    )
    assert r2.status_code == 200, r2.text

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(bill_id),
                    ChangeLog.entity == "bill",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    ops = [row.op for row in rows]
    assert "post" in ops, f"Expected 'post' in change_log ops, got {ops}"
    assert "void" in ops, f"Expected 'void' in change_log ops, got {ops}"
    assert len(rows) >= 2


# ---------------------------------------------------------------------------
# Tenant isolation — tenant B cannot post tenant A's bill → 404
# ---------------------------------------------------------------------------


async def test_bill_post_tenant_isolation(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    """A bill belonging to tenant A is not accessible by tenant B."""
    # Create bill under default tenant (tenant A)
    body = await _create_bill(api_client, bill_deps)
    bill_id = body["id"]
    version = body["version"]

    # Attempt to post from a different (tenant B) context.
    # Override SAEBOOKS_DEV_TENANT_ID to a random UUID to simulate tenant B.
    tenant_b_id = str(uuid.uuid4())
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        r = await api_client.post(
            f"/api/v1/bills/{bill_id}/post",
            headers={"If-Match": str(version)},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r.status_code == 404
