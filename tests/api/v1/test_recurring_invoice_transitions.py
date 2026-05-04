"""Status-transition tests for /api/v1/recurring_invoices/{id}/pause|resume|end.

Covers:
* POST /{id}/pause happy path — ACTIVE → PAUSED, version bumped
* POST /{id}/resume happy path — PAUSED → ACTIVE, version bumped
* POST /{id}/end happy path — ACTIVE → ENDED, version bumped
* POST /{id}/end on already-ENDED — 422
* POST /{id}/pause with stale If-Match — 409 with current state in body
* change_log entries created for each transition
"""
from __future__ import annotations

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
async def ri_deps() -> dict[str, str]:
    """Return contact_id and income account_id from seeded data."""
    async with AsyncSessionLocal() as session:
        contact = (
            await session.execute(
                select(Contact).where(Contact.archived_at.is_(None), Contact.tenant_id == DEFAULT_TENANT_ID).limit(1)
            )
        ).scalars().first()
        account = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
    assert contact is not None, "Test DB has no contact"
    assert account is not None, "Test DB has no INCOME account"
    return {
        "contact_id": str(contact.id),
        "account_id": str(account.id),
    }


def _ri_payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "name": "Transition Test RI",
        "contact_id": deps["contact_id"],
        "frequency": "MONTHLY",
        "next_run": "2026-06-01",
        "lines": [
            {
                "description": "Test line",
                "account_id": deps["account_id"],
                "quantity": "1",
                "unit_price": "100.00",
                "discount_pct": "0",
            }
        ],
    }
    base.update(overrides)
    return base


async def _create_ri(client: AsyncClient, deps: dict[str, str]) -> dict:
    """Create a recurring invoice (ACTIVE by default) and return the body."""
    r = await client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# PAUSE happy path
# ---------------------------------------------------------------------------


async def test_pause_happy(api_client: AsyncClient, ri_deps: dict[str, str]) -> None:
    """ACTIVE → PAUSED: returns 200 with PAUSED status and bumped version."""
    body = await _create_ri(api_client, ri_deps)
    ri_id = body["id"]
    version = body["version"]
    assert body["status"] == "ACTIVE"

    r = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/pause",
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    paused = r.json()
    assert paused["status"] == "PAUSED"
    assert paused["version"] == version + 1
    assert paused["id"] == ri_id


# ---------------------------------------------------------------------------
# RESUME happy path
# ---------------------------------------------------------------------------


async def test_resume_happy(api_client: AsyncClient, ri_deps: dict[str, str]) -> None:
    """PAUSED → ACTIVE: returns 200 with ACTIVE status and bumped version."""
    body = await _create_ri(api_client, ri_deps)
    ri_id = body["id"]

    # First pause
    r1 = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/pause",
        headers={"If-Match": str(body["version"])},
    )
    assert r1.status_code == 200, r1.text
    paused_version = r1.json()["version"]

    # Now resume
    r2 = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/resume",
        headers={"If-Match": str(paused_version)},
    )
    assert r2.status_code == 200, r2.text
    resumed = r2.json()
    assert resumed["status"] == "ACTIVE"
    assert resumed["version"] == paused_version + 1
    assert resumed["id"] == ri_id


# ---------------------------------------------------------------------------
# END happy path
# ---------------------------------------------------------------------------


async def test_end_happy(api_client: AsyncClient, ri_deps: dict[str, str]) -> None:
    """ACTIVE → ENDED: returns 200 with ENDED status and bumped version."""
    body = await _create_ri(api_client, ri_deps)
    ri_id = body["id"]
    version = body["version"]

    r = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/end",
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    ended = r.json()
    assert ended["status"] == "ENDED"
    assert ended["version"] == version + 1
    assert ended["id"] == ri_id


# ---------------------------------------------------------------------------
# Double-end → 422
# ---------------------------------------------------------------------------


async def test_double_end_422(api_client: AsyncClient, ri_deps: dict[str, str]) -> None:
    """Ending an already-ENDED recurring invoice must return 422."""
    body = await _create_ri(api_client, ri_deps)
    ri_id = body["id"]

    # First end
    r1 = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/end",
        headers={"If-Match": str(body["version"])},
    )
    assert r1.status_code == 200, r1.text
    ended_version = r1.json()["version"]

    # Second end on same RI
    r2 = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/end",
        headers={"If-Match": str(ended_version)},
    )
    assert r2.status_code == 422


# ---------------------------------------------------------------------------
# Stale version → 409
# ---------------------------------------------------------------------------


async def test_stale_version_409(
    api_client: AsyncClient, ri_deps: dict[str, str]
) -> None:
    """Pausing with a stale If-Match must return 409 with current state."""
    body = await _create_ri(api_client, ri_deps)
    ri_id = body["id"]

    r = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/pause",
        headers={"If-Match": "99"},
    )
    assert r.status_code == 409
    conflict = r.json()
    assert conflict["detail"] == "version mismatch"
    assert conflict["current"]["id"] == ri_id


# ---------------------------------------------------------------------------
# change_log entries for each transition
# ---------------------------------------------------------------------------


async def test_change_log_entries(
    api_client: AsyncClient, ri_deps: dict[str, str]
) -> None:
    """Create + pause + resume + end produces 4 change_log rows in order."""
    import uuid

    body = await _create_ri(api_client, ri_deps)
    ri_id = body["id"]
    ri_uuid = uuid.UUID(ri_id)

    # pause
    r1 = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/pause",
        headers={"If-Match": str(body["version"])},
    )
    assert r1.status_code == 200, r1.text
    paused_version = r1.json()["version"]

    # resume
    r2 = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/resume",
        headers={"If-Match": str(paused_version)},
    )
    assert r2.status_code == 200, r2.text
    resumed_version = r2.json()["version"]

    # end
    r3 = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/end",
        headers={"If-Match": str(resumed_version)},
    )
    assert r3.status_code == 200, r3.text

    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(ChangeLog)
                    .where(ChangeLog.entity_id == ri_uuid)
                    .order_by(ChangeLog.version)
                )
            )
            .scalars()
            .all()
        )

    ops = [row.op for row in rows]
    # Must contain created, paused, resumed, ended in order
    assert "created" in ops
    assert "paused" in ops
    assert "resumed" in ops
    assert "ended" in ops
    created_idx = ops.index("created")
    paused_idx = ops.index("paused")
    resumed_idx = ops.index("resumed")
    ended_idx = ops.index("ended")
    assert created_idx < paused_idx < resumed_idx < ended_idx
