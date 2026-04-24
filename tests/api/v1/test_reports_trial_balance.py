"""Tier-5 report tests — /api/v1/reports/trial_balance (cycle 27).

5 tests:
* test_trial_balance_empty
* test_trial_balance_credit_account_balance
* test_trial_balance_debit_account_balance
* test_trial_balance_balanced_assertion
* test_trial_balance_tenant_isolation
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType


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
async def gl_accounts() -> dict[str, str]:
    """Return one account ID per relevant AccountType for building JE payloads."""
    async with AsyncSessionLocal() as session:
        result: dict[str, str] = {}
        for at in (
            AccountType.INCOME,
            AccountType.EXPENSE,
            AccountType.ASSET,
        ):
            row = (
                await session.execute(
                    select(Account).where(
                        Account.archived_at.is_(None),
                        Account.account_type == at,
                        Account.is_header.is_(False),
                    ).limit(1)
                )
            ).scalars().first()
            assert row is not None, f"Test DB has no non-header {at.value} account"
            result[at.value] = str(row.id)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_and_post_je(
    client: AsyncClient,
    entry_date: str,
    lines: list[dict],
) -> dict:
    """Create a DRAFT JE then PATCH to POSTED. Return posted body."""
    r = await client.post(
        "/api/v1/journal_entries",
        json={
            "entry_date": entry_date,
            "narration": "Trial balance test entry",
            "lines": lines,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    je_id = body["id"]
    version = body["version"]

    r2 = await client.patch(
        f"/api/v1/journal_entries/{je_id}",
        json={"status": "POSTED"},
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_trial_balance_empty(api_client: AsyncClient) -> None:
    """No POSTED JEs before 1998-12-31 → empty accounts, balanced=True."""
    r = await api_client.get(
        "/api/v1/reports/trial_balance",
        params={"as_of_date": "1998-12-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["as_of_date"] == "1998-12-31"
    assert body["accounts"] == []
    assert body["total_debits"] == 0.0
    assert body["total_credits"] == 0.0
    assert body["balanced"] is True


async def test_trial_balance_credit_account_balance(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """JE crediting an INCOME account → that account appears with credit_total > 0."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client,
        "2027-01-10",
        lines=[
            {"account_id": asset_id, "debit": "1500.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "1500.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/trial_balance",
        params={"as_of_date": "2027-01-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    income_lines = [a for a in body["accounts"] if a["account_id"] == income_id]
    assert income_lines, "INCOME account not found in trial balance"
    assert income_lines[0]["credit_total"] >= 1500.0
    # credit-normal account: credit_total > debit_total → negative balance
    assert income_lines[0]["balance"] < 0.0


async def test_trial_balance_debit_account_balance(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """JE debiting an ASSET account → that account appears with debit_total > 0."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client,
        "2027-02-05",
        lines=[
            {"account_id": asset_id, "debit": "2200.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "2200.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/trial_balance",
        params={"as_of_date": "2027-02-28"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    asset_lines = [a for a in body["accounts"] if a["account_id"] == asset_id]
    assert asset_lines, "ASSET account not found in trial balance"
    assert asset_lines[0]["debit_total"] >= 2200.0
    # debit-normal account: debit_total > credit_total → positive balance
    assert asset_lines[0]["balance"] > 0.0


async def test_trial_balance_balanced_assertion(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """A balanced GL → total_debits == total_credits and balanced=True."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    amount = "3333.00"
    await _create_and_post_je(
        api_client,
        "2027-03-15",
        lines=[
            {"account_id": asset_id, "debit": amount, "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": amount},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/trial_balance",
        params={"as_of_date": "2027-03-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Every properly posted JE keeps the GL in balance
    assert body["balanced"] is True, "GL should be balanced after posting balanced JE"
    assert abs(body["total_debits"] - body["total_credits"]) < 0.01, (
        f"total_debits ({body['total_debits']}) != total_credits ({body['total_credits']})"
    )


async def test_trial_balance_tenant_isolation(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """Tenant B cannot see tenant A's GL lines in the trial balance."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    # Post a distinctive-amount JE under the default tenant (A)
    await _create_and_post_je(
        api_client,
        "2027-04-01",
        lines=[
            {"account_id": asset_id, "debit": "7171.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "7171.00"},
        ],
    )

    # Query as tenant B
    tenant_b_id = str(uuid.uuid4())
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        r = await api_client.get(
            "/api/v1/reports/trial_balance",
            params={"as_of_date": "2027-04-30"},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r.status_code == 200, r.text
    body = r.json()
    # Tenant B should see no accounts (their company has no JEs)
    debit_totals = [a["debit_total"] for a in body["accounts"]]
    assert 7171.0 not in debit_totals, (
        "Tenant B should not see tenant A's GL lines in trial balance"
    )
