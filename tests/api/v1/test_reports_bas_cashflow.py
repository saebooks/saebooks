"""Tier-5 report tests — /api/v1/reports/bas_summary + /cashflow.

9 tests:
* test_bas_empty
* test_bas_taxable_sale
* test_bas_gst_free_sale
* test_bas_expense
* test_bas_net_gst_remit
* test_bas_tenant_isolation
* test_cashflow_empty
* test_cashflow_operating
* test_cashflow_investing
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
from saebooks.models.tax_code import TaxCode


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
    """Return one account ID per relevant AccountType."""
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
                    select(Account)
                    .where(
                        Account.archived_at.is_(None),
                        Account.account_type == at,
                        Account.is_header.is_(False),
                    )
                    .limit(1)
                )
            ).scalars().first()
            assert row is not None, f"Test DB has no non-header {at.value} account"
            result[at.value] = str(row.id)
    return result


@pytest.fixture
async def tax_codes() -> dict[str, str]:
    """Return tax code IDs keyed by reporting_type from seeded AU tax codes."""
    async with AsyncSessionLocal() as session:
        gst_row = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.archived_at.is_(None),
                    TaxCode.code == "GST",
                )
            )
        ).scalars().first()
        fre_row = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.archived_at.is_(None),
                    TaxCode.code == "FRE",
                )
            )
        ).scalars().first()
        assert gst_row is not None, "Seed tax code GST not found"
        assert fre_row is not None, "Seed tax code FRE not found"
        return {
            "taxable": str(gst_row.id),
            "gst_free": str(fre_row.id),
        }


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
            "narration": "Test BAS/cashflow entry",
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
# BAS tests
# ---------------------------------------------------------------------------


async def test_bas_empty(api_client: AsyncClient) -> None:
    """No POSTED JEs in range → all BAS fields zero."""
    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "1997-01-01", "to_date": "1997-12-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["from_date"] == "1997-01-01"
    assert body["to_date"] == "1997-12-31"
    assert body["g1_total_sales"] == 0.0
    assert body["g2_export_sales"] == 0.0
    assert body["g3_other_gst_free_sales"] == 0.0
    assert body["g10_capital_acquisitions"] == 0.0
    assert body["g11_other_acquisitions"] == 0.0
    assert body["label_1a_gst_on_sales"] == 0.0
    assert body["label_1b_gst_on_purchases"] == 0.0
    assert body["net_gst"] == 0.0
    # remit_or_refund can be either when net_gst == 0; just assert it's present
    assert body["remit_or_refund"] in ("REMIT", "REFUND")


async def test_bas_taxable_sale(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """JE crediting a taxable INCOME account → G1 and 1A computed."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    amount = "1000.00"
    await _create_and_post_je(
        api_client,
        "2027-01-15",
        lines=[
            {
                "account_id": asset_id,
                "debit": amount,
                "credit": "0",
            },
            {
                "account_id": income_id,
                "debit": "0",
                "credit": amount,
                "tax_code_id": gst_id,
            },
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "2027-01-01", "to_date": "2027-01-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["g1_total_sales"] >= 1000.0, f"Expected g1 >= 1000, got {body['g1_total_sales']}"
    # 1A = G1 × 10%; for exactly 1000 that's 100.00
    assert body["label_1a_gst_on_sales"] >= 100.0, (
        f"Expected 1a >= 100, got {body['label_1a_gst_on_sales']}"
    )
    assert body["g3_other_gst_free_sales"] == 0.0


async def test_bas_gst_free_sale(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """JE crediting a gst_free INCOME account → G3 populated, G1 and 1A unaffected."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    fre_id = tax_codes["gst_free"]

    amount = "500.00"
    await _create_and_post_je(
        api_client,
        "2027-02-10",
        lines=[
            {
                "account_id": asset_id,
                "debit": amount,
                "credit": "0",
            },
            {
                "account_id": income_id,
                "debit": "0",
                "credit": amount,
                "tax_code_id": fre_id,
            },
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "2027-02-01", "to_date": "2027-02-28"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["g3_other_gst_free_sales"] >= 500.0, (
        f"Expected g3 >= 500, got {body['g3_other_gst_free_sales']}"
    )
    # GST-free sales contribute nothing to 1A via this code
    # (no taxable line in this JE so g1 and 1a should be unchanged from empty)
    assert body["label_1a_gst_on_sales"] == pytest.approx(
        body["g1_total_sales"] * 0.10, abs=0.02
    ), "1A should always equal G1 × 10%"


async def test_bas_expense(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """JE debiting a taxable EXPENSE account → G11 and 1B computed."""
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    amount = "1100.00"
    await _create_and_post_je(
        api_client,
        "2027-03-05",
        lines=[
            {
                "account_id": expense_id,
                "debit": amount,
                "credit": "0",
                "tax_code_id": gst_id,
            },
            {
                "account_id": asset_id,
                "debit": "0",
                "credit": amount,
            },
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "2027-03-01", "to_date": "2027-03-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["g11_other_acquisitions"] >= 1100.0, (
        f"Expected g11 >= 1100, got {body['g11_other_acquisitions']}"
    )
    # 1B = G11 × 1/11; for exactly 1100 that's 100.00
    assert body["label_1b_gst_on_purchases"] >= 100.0, (
        f"Expected 1b >= 100, got {body['label_1b_gst_on_purchases']}"
    )


async def test_bas_net_gst_remit(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """1A > 1B → remit_or_refund == REMIT."""
    income_id = gl_accounts[AccountType.INCOME.value]
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    # Large taxable sale
    await _create_and_post_je(
        api_client,
        "2027-04-15",
        lines=[
            {"account_id": asset_id, "debit": "5000.00", "credit": "0"},
            {
                "account_id": income_id,
                "debit": "0",
                "credit": "5000.00",
                "tax_code_id": gst_id,
            },
        ],
    )
    # Small taxable expense
    await _create_and_post_je(
        api_client,
        "2027-04-20",
        lines=[
            {
                "account_id": expense_id,
                "debit": "110.00",
                "credit": "0",
                "tax_code_id": gst_id,
            },
            {"account_id": asset_id, "debit": "0", "credit": "110.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={"from_date": "2027-04-01", "to_date": "2027-04-30"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["label_1a_gst_on_sales"] > body["label_1b_gst_on_purchases"], (
        "1A should exceed 1B in this scenario"
    )
    assert body["net_gst"] > 0.0
    assert body["remit_or_refund"] == "REMIT"


async def test_bas_tenant_isolation(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """Tenant B cannot see tenant A's taxable sales in the BAS report."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    # Post a JE under the default tenant (A)
    await _create_and_post_je(
        api_client,
        "2027-05-10",
        lines=[
            {"account_id": asset_id, "debit": "7777.00", "credit": "0"},
            {
                "account_id": income_id,
                "debit": "0",
                "credit": "7777.00",
                "tax_code_id": gst_id,
            },
        ],
    )

    # Query BAS as tenant B
    tenant_b_id = str(uuid.uuid4())
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b_id
    try:
        r = await api_client.get(
            "/api/v1/reports/bas_summary",
            params={"from_date": "2027-05-01", "to_date": "2027-05-31"},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    # Tenant B has no company → 404, which proves isolation.
    assert r.status_code in (200, 404), r.text
    if r.status_code == 200:
        body = r.json()
        assert body["g1_total_sales"] < 7777.0, (
            "Tenant B should not see tenant A's taxable sales in BAS"
        )


async def test_bas_mid_quarter_registration_split(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
    tax_codes: dict[str, str],
) -> None:
    """Mid-quarter GST registration: pre-reg sales in G1 but excluded from 1A.

    Scenario (regression fix):
      Quarter     2027-07-01 to 2027-09-30
      Registration effective 2027-08-01
      Pre-reg sale  2027-07-15  AUD 2000 taxable
      Post-reg sale 2027-08-10  AUD 3000 taxable
    Expected:
      g1_total_sales = 5000  (all sales disclosed)
      g1_pre_registration = 2000
      g1_post_registration = 3000
      label_1a_gst_on_sales = 300  (3000 x 10% — pre-reg excluded)
    """
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    gst_id = tax_codes["taxable"]

    # Pre-registration sale (2027-07-15)
    await _create_and_post_je(
        api_client,
        "2027-07-15",
        lines=[
            {"account_id": asset_id, "debit": "2000.00", "credit": "0"},
            {
                "account_id": income_id,
                "debit": "0",
                "credit": "2000.00",
                "tax_code_id": gst_id,
            },
        ],
    )

    # Post-registration sale (2027-08-10)
    await _create_and_post_je(
        api_client,
        "2027-08-10",
        lines=[
            {"account_id": asset_id, "debit": "3000.00", "credit": "0"},
            {
                "account_id": income_id,
                "debit": "0",
                "credit": "3000.00",
                "tax_code_id": gst_id,
            },
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/bas_summary",
        params={
            "from_date": "2027-07-01",
            "to_date": "2027-09-30",
            "registration_effective_date": "2027-08-01",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["registration_effective_date"] == "2027-08-01"
    assert body["g1_total_sales"] >= 5000.0, f"g1_total_sales={body['g1_total_sales']}"
    assert body["g1_pre_registration"] >= 2000.0, (
        f"g1_pre_registration={body['g1_pre_registration']}"
    )
    assert body["g1_post_registration"] >= 3000.0, (
        f"g1_post_registration={body['g1_post_registration']}"
    )
    # 1A must be based only on post-registration sales (3000 x 10% = 300)
    assert body["label_1a_gst_on_sales"] == pytest.approx(
        body["g1_post_registration"] * 0.10, abs=0.02
    ), f"1A should be post-reg G1 x 10%, got {body['label_1a_gst_on_sales']}"
    assert body["label_1a_gst_on_sales"] < body["g1_total_sales"] * 0.10, (
        "1A must be less than it would be if all sales were taxable"
    )


# ---------------------------------------------------------------------------
# Cashflow tests
# ---------------------------------------------------------------------------


async def test_cashflow_empty(api_client: AsyncClient) -> None:
    """No POSTED JEs in range → net_change == 0."""
    r = await api_client.get(
        "/api/v1/reports/cashflow",
        params={"from_date": "1996-01-01", "to_date": "1996-12-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["from_date"] == "1996-01-01"
    assert body["to_date"] == "1996-12-31"
    assert body["operating"]["net_profit"] == 0.0
    assert body["operating"]["total_operating"] == 0.0
    assert body["operating"]["adjustments"] == []
    assert body["investing"]["total_investing"] == 0.0
    assert body["financing"]["total_financing"] == 0.0
    assert body["net_change"] == 0.0
    assert abs(body["closing_cash"] - body["opening_cash"]) < 0.01


async def test_cashflow_operating(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
) -> None:
    """Income and expense JEs drive net_profit in the operating section."""
    income_id = gl_accounts[AccountType.INCOME.value]
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    # Income 3000
    await _create_and_post_je(
        api_client,
        "2028-01-10",
        lines=[
            {"account_id": asset_id, "debit": "3000.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "3000.00"},
        ],
    )
    # Expense 1000
    await _create_and_post_je(
        api_client,
        "2028-01-20",
        lines=[
            {"account_id": expense_id, "debit": "1000.00", "credit": "0"},
            {"account_id": asset_id, "debit": "0", "credit": "1000.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/cashflow",
        params={"from_date": "2028-01-01", "to_date": "2028-01-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["operating"]["net_profit"] >= 2000.0, (
        f"Expected net_profit >= 2000, got {body['operating']['net_profit']}"
    )
    assert body["operating"]["total_operating"] == body["operating"]["net_profit"]


async def test_cashflow_investing(
    api_client: AsyncClient,
    gl_accounts: dict[str, str],
) -> None:
    """A non-cash ASSET debit shows as an outflow in the investing section."""
    # We need a non-cash asset account — use the seeded fixed-asset or any
    # ASSET account whose name/code does NOT contain "cash" or "bank".
    async with AsyncSessionLocal() as session:
        fixed_asset_row = (
            await session.execute(
                select(Account)
                .where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
            )
        ).scalars().all()

    # Find one that is not a cash/bank account
    non_cash_asset = None
    for acct in fixed_asset_row:
        name_lower = acct.name.lower()
        code_lower = acct.code.lower()
        if not any(kw in name_lower or kw in code_lower for kw in ("cash", "bank")):
            non_cash_asset = str(acct.id)
            break

    if non_cash_asset is None:
        pytest.skip("No non-cash ASSET account found in test DB")

    income_id = gl_accounts[AccountType.INCOME.value]

    # DR non-cash asset, CR income (simulates asset purchase financed by income)
    await _create_and_post_je(
        api_client,
        "2028-02-05",
        lines=[
            {"account_id": non_cash_asset, "debit": "4000.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "4000.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/cashflow",
        params={"from_date": "2028-02-01", "to_date": "2028-02-28"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Asset purchase is an outflow → asset_purchases is negative (outflow convention)
    assert body["investing"]["asset_purchases"] <= -4000.0, (
        f"Expected asset_purchases <= -4000 (outflow), got {body['investing']['asset_purchases']}"
    )
    assert body["investing"]["total_investing"] <= -4000.0, (
        f"Expected total_investing <= -4000, got {body['investing']['total_investing']}"
    )
