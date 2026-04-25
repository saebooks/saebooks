"""Tier-5 financial report tests — /api/v1/reports/profit_loss + /balance_sheet.

10 tests:
* test_pnl_empty
* test_pnl_income_line
* test_pnl_expense_line
* test_pnl_net_profit_calculation
* test_pnl_date_range_filter
* test_pnl_tenant_isolation
* test_balance_sheet_empty
* test_balance_sheet_asset_account
* test_balance_sheet_cumulative
* test_balance_sheet_tenant_isolation
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token, DEFAULT_TENANT_ID
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
            AccountType.LIABILITY,
            AccountType.EQUITY,
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
    """Create a DRAFT journal entry then PATCH it to POSTED. Return posted body."""
    r = await client.post(
        "/api/v1/journal_entries",
        json={
            "entry_date": entry_date,
            "narration": "Test GL entry",
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


def _balanced_lines(
    debit_account_id: str,
    credit_account_id: str,
    amount: str = "1000.00",
) -> list[dict]:
    """Return a balanced two-line JE payload (debit + credit)."""
    return [
        {"account_id": debit_account_id, "debit": amount, "credit": "0"},
        {"account_id": credit_account_id, "debit": "0", "credit": amount},
    ]


# ---------------------------------------------------------------------------
# P&L tests
# ---------------------------------------------------------------------------


async def test_pnl_empty(api_client: AsyncClient) -> None:
    """No POSTED JEs in range → all zeros, valid structure."""
    r = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={"from_date": "1999-01-01", "to_date": "1999-12-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from_date"] == "1999-01-01"
    assert body["to_date"] == "1999-12-31"
    assert body["income"]["total_income"] == 0.0
    assert body["expenses"]["total_expenses"] == 0.0
    assert body["net_profit"] == 0.0
    assert body["income"]["INCOME"] == []
    assert body["income"]["OTHER_INCOME"] == []
    assert body["expenses"]["EXPENSE"] == []
    assert body["expenses"]["COST_OF_SALES"] == []
    assert body["expenses"]["OTHER_EXPENSE"] == []


async def test_pnl_income_line(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """POST a JE crediting an INCOME account → appears in income section."""
    entry_date = "2026-02-15"
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client,
        entry_date,
        # Credit income, debit asset (e.g. AR)
        lines=_balanced_lines(asset_id, income_id, "2500.00"),
    )

    r = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={"from_date": "2026-02-01", "to_date": "2026-02-28"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Income account appears in INCOME list
    income_lines = body["income"]["INCOME"]
    assert any(
        line["account_id"] == income_id for line in income_lines
    ), "INCOME account not found in P&L income section"
    assert body["income"]["total_income"] > 0


async def test_pnl_expense_line(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """POST a JE debiting an EXPENSE account → appears in expenses section."""
    entry_date = "2026-03-10"
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client,
        entry_date,
        # Debit expense, credit asset (e.g. pay cash)
        lines=_balanced_lines(expense_id, asset_id, "750.00"),
    )

    r = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={"from_date": "2026-03-01", "to_date": "2026-03-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    expense_lines = body["expenses"]["EXPENSE"]
    assert any(
        line["account_id"] == expense_id for line in expense_lines
    ), "EXPENSE account not found in P&L expenses section"
    assert body["expenses"]["total_expenses"] > 0


async def test_pnl_net_profit_calculation(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """Income > expenses → positive net_profit; net_profit = total_income - total_expenses.

    Uses year 2093 which has no seed data or prior test pollution.
    """
    entry_date = "2093-04-05"
    income_id = gl_accounts[AccountType.INCOME.value]
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    # Post income of 5000
    await _create_and_post_je(
        api_client,
        entry_date,
        lines=_balanced_lines(asset_id, income_id, "5000.00"),
    )
    # Post expense of 2000
    await _create_and_post_je(
        api_client,
        entry_date,
        lines=_balanced_lines(expense_id, asset_id, "2000.00"),
    )

    r = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={"from_date": "2093-04-01", "to_date": "2093-04-30"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    ti = body["income"]["total_income"]
    te = body["expenses"]["total_expenses"]
    np_ = body["net_profit"]

    assert ti >= 5000.0, f"Expected at least 5000 income, got {ti}"
    assert te >= 2000.0, f"Expected at least 2000 expenses, got {te}"
    assert np_ > 0.0, "Expected positive net_profit"
    assert abs(np_ - (ti - te)) < 0.01, "net_profit should equal total_income - total_expenses"


async def test_pnl_date_range_filter(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """JE outside the date range is excluded from P&L."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    # Post JE in May 2025 (outside our query range)
    await _create_and_post_je(
        api_client,
        "2025-05-15",
        lines=_balanced_lines(asset_id, income_id, "9999.00"),
    )

    # Query only June 2025
    r = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={"from_date": "2025-06-01", "to_date": "2025-06-30"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # The May JE should not appear in June totals
    for line in body["income"]["INCOME"]:
        # If the account does appear it must not include the 9999 amount from May
        # (there may be other JEs for this account from other tests in June)
        pass
    # The cleanest assertion: the total for this specific window should not
    # include the 9999 amount we just posted in May.
    # We verify that no income line has amount == 9999.0 (a distinctive amount).
    income_amounts = [line["amount"] for line in body["income"]["INCOME"]]
    assert 9999.0 not in income_amounts, (
        "May JE should not appear in June P&L date range"
    )


async def test_pnl_tenant_isolation(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """Tenant B cannot see tenant A's GL lines in the P&L report."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    # Post a JE under the default tenant (A)
    await _create_and_post_je(
        api_client,
        "2026-01-20",
        lines=_balanced_lines(asset_id, income_id, "8888.00"),
    )

    # Query the P&L as tenant B
    tenant_b_id = str(uuid.uuid4())
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        r = await api_client.get(
            "/api/v1/reports/profit_loss",
            params={"from_date": "2026-01-01", "to_date": "2026-01-31"},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r.status_code == 200, r.text
    body = r.json()
    # Tenant B's report should not include 8888 from tenant A
    income_amounts = [line["amount"] for line in body["income"]["INCOME"]]
    assert 8888.0 not in income_amounts, (
        "Tenant B should not see tenant A's income in P&L"
    )


# ---------------------------------------------------------------------------
# Balance Sheet tests
# ---------------------------------------------------------------------------


async def test_balance_sheet_empty(api_client: AsyncClient) -> None:
    """No POSTED JEs up to as_of_date → zeros, balanced=True."""
    r = await api_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "1998-12-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["as_of_date"] == "1998-12-31"
    assert body["assets"]["total_assets"] == 0.0
    assert body["liabilities"]["total_liabilities"] == 0.0
    assert body["equity"]["total_equity"] == 0.0
    assert body["balanced"] is True
    assert body["difference"] == 0.0


async def test_balance_sheet_asset_account(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """JE debiting an ASSET account → balance > 0 in assets section."""
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]

    await _create_and_post_je(
        api_client,
        "2026-06-01",
        # Debit asset, credit income
        lines=_balanced_lines(asset_id, income_id, "3000.00"),
    )

    r = await api_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "2026-06-30"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    asset_lines = body["assets"]["ASSET"]
    matching = [line for line in asset_lines if line["account_id"] == asset_id]
    assert matching, "ASSET account not found in balance sheet assets section"
    # The specific account we debited should carry a positive balance
    # (total_assets may be negative due to pre-existing DB data from other tests)
    assert matching[0]["balance"] > 0, (
        f"Expected positive balance for ASSET account {asset_id}, got {matching[0]['balance']}"
    )


async def test_balance_sheet_cumulative(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """JEs before as_of_date included; JEs after excluded."""
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]

    # Post JE in July 2025 (should appear in a July 31 BS)
    await _create_and_post_je(
        api_client,
        "2025-07-10",
        lines=_balanced_lines(asset_id, income_id, "4000.00"),
    )
    # Post JE in August 2025 (should NOT appear in July 31 BS)
    await _create_and_post_je(
        api_client,
        "2025-08-05",
        lines=_balanced_lines(asset_id, income_id, "7777.00"),
    )

    # Balance sheet as at 2025-07-31 — includes July JE, excludes August JE
    r_july = await api_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "2025-07-31"},
    )
    assert r_july.status_code == 200, r_july.text
    body_july = r_july.json()

    # Balance sheet as at 2025-08-31 — includes both JEs
    r_aug = await api_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "2025-08-31"},
    )
    assert r_aug.status_code == 200, r_aug.text
    body_aug = r_aug.json()

    # August total should be larger (includes both entries)
    assert body_aug["assets"]["total_assets"] >= body_july["assets"]["total_assets"], (
        "August BS total_assets should be >= July (August includes an additional JE)"
    )
    # The August BS should include the 7777 JE; July should not.
    # We check that August total is at least 7777 more than July for this asset.
    july_asset_bal = sum(
        line["balance"] for line in body_july["assets"]["ASSET"]
        if line["account_id"] == asset_id
    )
    aug_asset_bal = sum(
        line["balance"] for line in body_aug["assets"]["ASSET"]
        if line["account_id"] == asset_id
    )
    assert aug_asset_bal >= july_asset_bal + 7777.0 - 0.01, (
        "August cumulative balance should include the August 7777 JE"
    )


async def test_balance_sheet_tenant_isolation(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """Tenant B cannot see tenant A's GL lines in the balance sheet."""
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]

    # Post a JE under the default tenant (A)
    await _create_and_post_je(
        api_client,
        "2026-09-01",
        lines=_balanced_lines(asset_id, income_id, "6666.00"),
    )

    # Query the balance sheet as tenant B
    tenant_b_id = str(uuid.uuid4())
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        r = await api_client.get(
            "/api/v1/reports/balance_sheet",
            params={"as_of_date": "2026-09-30"},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r.status_code == 200, r.text
    body = r.json()
    # Tenant B sees no assets from tenant A (their company has no JEs)
    asset_balances = [line["balance"] for line in body["assets"]["ASSET"]]
    assert 6666.0 not in asset_balances, (
        "Tenant B should not see tenant A's assets in balance sheet"
    )


# ---------------------------------------------------------------------------
# Bug 2 regression: Balance sheet must include Current Year Earnings
# ---------------------------------------------------------------------------


async def test_balance_sheet_includes_cye_line(api_client: AsyncClient) -> None:
    """Balance sheet equity section must always contain a 'Current Year Earnings' line.

    The CYE line synthesises unposted P&L into the equity section so that
    Assets = Liabilities + Equity for any balanced ledger.
    """
    r = await api_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "1998-12-31"},  # no data → CYE should be 0
    )
    assert r.status_code == 200, r.text
    body = r.json()
    equity_lines = body["equity"]["EQUITY"]
    cye_lines = [ln for ln in equity_lines if ln.get("code") == "CYE"]
    assert cye_lines, (
        "Expected a 'Current Year Earnings' line (code=CYE) in the equity section"
    )


async def test_balance_sheet_balances_with_income_only(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """With INCOME and ASSETS posted, the balance sheet must balance.

    Scenario (uses year 2091 to avoid pollution):
      ASSET  +95,425  (debit)
      INCOME -86,750  (credit-normal)
      LIAB    -8,675  (credit)

    Expected: Assets 95,425 = Liabilities 8,675 + Equity (CYE) 86,750
    balanced must be True, difference must be < 0.01.
    """
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]
    liability_id = gl_accounts[AccountType.LIABILITY.value]

    # Post: debit ASSET 86,750, credit INCOME 86,750
    await _create_and_post_je(
        api_client,
        "2091-03-01",
        lines=_balanced_lines(asset_id, income_id, "86750.00"),
    )

    # Post: debit ASSET 8,675, credit LIABILITY 8,675
    await _create_and_post_je(
        api_client,
        "2091-03-02",
        lines=[
            {"account_id": asset_id, "debit": "8675.00", "credit": "0"},
            {"account_id": liability_id, "debit": "0", "credit": "8675.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "2091-03-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # The BS must balance (Assets = Liabilities + Equity with CYE).
    assert body["balanced"] is True, (
        f"Balance sheet is not balanced: difference={body['difference']}, body={body}"
    )
    assert body["difference"] < 0.01, (
        f"Expected difference < 0.01, got {body['difference']}"
    )

    # CYE line must exist and carry the income value.
    equity_lines = body["equity"]["EQUITY"]
    cye_lines = [ln for ln in equity_lines if ln.get("code") == "CYE"]
    assert cye_lines, "CYE line missing from equity section"
    assert cye_lines[0]["balance"] >= 86750.0 - 0.01, (
        f"CYE balance should include 86,750 income, got {cye_lines[0]['balance']}"
    )


async def test_balance_sheet_cye_zero_for_empty_period(api_client: AsyncClient) -> None:
    """An empty ledger period produces CYE = 0 and balanced = True."""
    r = await api_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "1995-12-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    equity_lines = body["equity"]["EQUITY"]
    cye_lines = [ln for ln in equity_lines if ln.get("code") == "CYE"]
    assert cye_lines, "CYE line must be present even when zero"
    assert cye_lines[0]["balance"] == 0.0, (
        f"Empty period CYE should be 0.0, got {cye_lines[0]['balance']}"
    )
    assert body["balanced"] is True
