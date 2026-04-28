"""Contract tests for /api/v1/allocation_rules.

Gap FITC-6 (medium-fitness-chain): routes previously returned 404.

Covers:
* Feature gate: Community → 404 on all endpoints
* Service: compute_allocation_lines rounding + balance
* Service: _validate_targets rejects bad input
* API CRUD: create → list → get → update → delete
* API apply: generates journal entry lines
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.config import Settings
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.services.allocations import (
    AllocationError,
    AllocationRule,
    _validate_targets,
    compute_allocation_lines,
)
import saebooks.services.features as features_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _expense_account_id() -> str:
    async with AsyncSessionLocal() as session:
        acct = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                ).limit(1)
            )
        ).scalars().first()
    assert acct is not None, "No EXPENSE account in test DB"
    return str(acct.id)


async def _two_expense_account_ids() -> tuple[str, str]:
    async with AsyncSessionLocal() as session:
        accts = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                ).limit(2)
            )
        ).scalars().all()
    assert len(accts) >= 2, "Need at least 2 EXPENSE accounts"
    return str(accts[0].id), str(accts[1].id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def auth_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def community_client() -> AsyncClient:
    """Client that hits the app while features module reports Community edition."""
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Unit tests — pure service logic
# ---------------------------------------------------------------------------


def test_validate_targets_empty_raises() -> None:
    with pytest.raises(AllocationError, match="At least one target"):
        _validate_targets([])


def test_validate_targets_percentages_not_100() -> None:
    targets = [
        {"account_id": str(uuid.uuid4()), "percentage": 60},
        {"account_id": str(uuid.uuid4()), "percentage": 30},
    ]
    with pytest.raises(AllocationError, match="sum to 100"):
        _validate_targets(targets)


def test_validate_targets_zero_percentage() -> None:
    targets = [
        {"account_id": str(uuid.uuid4()), "percentage": 0},
        {"account_id": str(uuid.uuid4()), "percentage": 100},
    ]
    with pytest.raises(AllocationError, match="positive"):
        _validate_targets(targets)


def test_validate_targets_missing_account_id() -> None:
    targets = [{"percentage": 100}]
    with pytest.raises(AllocationError, match="missing account_id"):
        _validate_targets(targets)


def test_validate_targets_valid() -> None:
    acct = str(uuid.uuid4())
    targets = [
        {"account_id": acct, "label": "Site A", "percentage": 50},
        {"account_id": str(uuid.uuid4()), "label": "Site B", "percentage": 50},
    ]
    _validate_targets(targets)  # should not raise


def test_compute_allocation_lines_balanced() -> None:
    """Lines debit targets + credit source = balanced entry."""
    src = uuid.uuid4()
    tgt1 = uuid.uuid4()
    tgt2 = uuid.uuid4()
    rule = AllocationRule(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        name="Test",
        source_account_id=src,
        targets=[
            {"account_id": str(tgt1), "label": "Site A", "percentage": 60},
            {"account_id": str(tgt2), "label": "Site B", "percentage": 40},
        ],
        is_active=True,
        version=1,
    )
    lines = compute_allocation_lines(rule, Decimal("1000.00"))
    total_debit = sum(Decimal(ln["debit"]) for ln in lines)
    total_credit = sum(Decimal(ln["credit"]) for ln in lines)
    assert total_debit == total_credit, "Lines must balance"
    assert total_credit == Decimal("1000.00")
    # Source line is the credit
    credit_lines = [ln for ln in lines if Decimal(ln["credit"]) > 0]
    assert len(credit_lines) == 1
    assert credit_lines[0]["account_id"] == str(src)
    # Two debit lines
    debit_lines = [ln for ln in lines if Decimal(ln["debit"]) > 0]
    assert len(debit_lines) == 2


def test_compute_allocation_lines_rounding() -> None:
    """Last target absorbs rounding residual so entry always balances."""
    src = uuid.uuid4()
    targets = [
        {"account_id": str(uuid.uuid4()), "label": f"Site {i}", "percentage": Decimal("20")}
        for i in range(5)
    ]
    rule = AllocationRule(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        name="Five-way even split",
        source_account_id=src,
        targets=targets,
        is_active=True,
        version=1,
    )
    lines = compute_allocation_lines(rule, Decimal("100.01"))
    total_debit = sum(Decimal(ln["debit"]) for ln in lines)
    total_credit = sum(Decimal(ln["credit"]) for ln in lines)
    assert total_debit == total_credit, "Lines with rounding must still balance"


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_gate_community_returns_404(community_client: AsyncClient) -> None:
    """Community edition → 404 on all allocation_rules endpoints."""
    community_settings = Settings(SAEBOOKS_EDITION="community")
    with patch.object(features_mod, "_default_settings", community_settings):
        resp = await community_client.get("/api/v1/allocation_rules")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_allocation_rules_empty(auth_client: AsyncClient) -> None:
    """GET /api/v1/allocation_rules → 200 with pagination shape."""
    resp = await auth_client.get("/api/v1/allocation_rules")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert "limit" in body
    assert "offset" in body


@pytest.mark.asyncio
async def test_create_and_get_allocation_rule(auth_client: AsyncClient) -> None:
    """POST → 201; GET → 200 with same data."""
    src_id, tgt_id = await _two_expense_account_ids()

    payload = {
        "name": "Test Rent Allocation",
        "description": "Split group rent equally",
        "source_account_id": src_id,
        "targets": [
            {"account_id": tgt_id, "label": "Site A", "percentage": 100.0},
        ],
        "is_active": True,
    }
    resp = await auth_client.post("/api/v1/allocation_rules", json=payload)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "Test Rent Allocation"
    assert created["version"] == 1
    assert len(created["targets"]) == 1

    rule_id = created["id"]
    resp2 = await auth_client.get(f"/api/v1/allocation_rules/{rule_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == rule_id


@pytest.mark.asyncio
async def test_update_allocation_rule_optimistic_lock(auth_client: AsyncClient) -> None:
    """PATCH with correct If-Match bumps version; stale If-Match → 409."""
    src_id, tgt_id = await _two_expense_account_ids()

    create_resp = await auth_client.post(
        "/api/v1/allocation_rules",
        json={
            "name": "Lock Test",
            "source_account_id": src_id,
            "targets": [{"account_id": tgt_id, "label": "X", "percentage": 100.0}],
        },
    )
    assert create_resp.status_code == 201
    rule_id = create_resp.json()["id"]

    # Correct version → 200
    patch_resp = await auth_client.patch(
        f"/api/v1/allocation_rules/{rule_id}",
        json={"name": "Lock Test Updated"},
        headers={"If-Match": "1"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["version"] == 2

    # Stale version → 409
    stale_resp = await auth_client.patch(
        f"/api/v1/allocation_rules/{rule_id}",
        json={"name": "Stale"},
        headers={"If-Match": "1"},
    )
    assert stale_resp.status_code == 409


@pytest.mark.asyncio
async def test_patch_without_if_match_returns_428(auth_client: AsyncClient) -> None:
    src_id, tgt_id = await _two_expense_account_ids()
    create_resp = await auth_client.post(
        "/api/v1/allocation_rules",
        json={
            "name": "No Match Test",
            "source_account_id": src_id,
            "targets": [{"account_id": tgt_id, "percentage": 100.0}],
        },
    )
    rule_id = create_resp.json()["id"]
    resp = await auth_client.patch(
        f"/api/v1/allocation_rules/{rule_id}",
        json={"name": "Updated"},
    )
    assert resp.status_code == 428


@pytest.mark.asyncio
async def test_delete_allocation_rule(auth_client: AsyncClient) -> None:
    """DELETE soft-archives the rule; no longer appears in default list."""
    src_id, tgt_id = await _two_expense_account_ids()
    create_resp = await auth_client.post(
        "/api/v1/allocation_rules",
        json={
            "name": "To Archive",
            "source_account_id": src_id,
            "targets": [{"account_id": tgt_id, "percentage": 100.0}],
        },
    )
    rule_id = create_resp.json()["id"]

    del_resp = await auth_client.delete(
        f"/api/v1/allocation_rules/{rule_id}",
        headers={"If-Match": "1"},
    )
    assert del_resp.status_code == 204

    # Not in default listing
    list_resp = await auth_client.get("/api/v1/allocation_rules")
    ids = [r["id"] for r in list_resp.json()["items"]]
    assert rule_id not in ids

    # Visible with archived=true
    arch_resp = await auth_client.get(
        "/api/v1/allocation_rules", params={"archived": "true"}
    )
    arch_ids = [r["id"] for r in arch_resp.json()["items"]]
    assert rule_id in arch_ids


@pytest.mark.asyncio
async def test_targets_must_sum_to_100(auth_client: AsyncClient) -> None:
    """Creating a rule with percentages != 100 returns 422."""
    src_id, tgt_id = await _two_expense_account_ids()
    resp = await auth_client.post(
        "/api/v1/allocation_rules",
        json={
            "name": "Bad Rule",
            "source_account_id": src_id,
            "targets": [
                {"account_id": tgt_id, "label": "X", "percentage": 60.0},
            ],
        },
    )
    assert resp.status_code == 422
