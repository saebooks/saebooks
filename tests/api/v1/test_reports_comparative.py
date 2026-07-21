"""Tier-5+ report tests — R7 comparative/prior-period query param.

Covers ``?compare=previous_period|previous_year`` on the JSON
/profit_loss, /balance_sheet, /trial_balance routes.

18 tests:
* test_pnl_compare_omitted_shape_unchanged
* test_pnl_compare_previous_period_math
* test_pnl_compare_previous_year_math
* test_pnl_compare_previous_year_leap_day
* test_pnl_compare_invalid_value_422
* test_bs_compare_omitted_shape_unchanged
* test_bs_compare_previous_year_math
* test_bs_compare_previous_period_is_prior_fy_close
* test_bs_compare_previous_period_uk_fy
* test_bs_compare_invalid_value_422
* test_tb_compare_omitted_shape_unchanged
* test_tb_compare_previous_year_math
* test_tb_compare_previous_period_is_prior_fy_close
* test_tb_compare_previous_period_uk_fy
* test_tb_compare_invalid_value_422
* test_pnl_compare_tenant_isolation
* test_bs_compare_tenant_isolation
* test_tb_compare_tenant_isolation

The Finding-1 (adversarial-review) fix -- BS/TB ``previous_period``
deriving the comparative date from the COMPANY's own
``fin_year_start_month``/``fin_year_start_day`` rather than the
AU-hardcoded 1 July -- is covered by the ``_uk_fy`` tests (a 6 April
anchor) plus the existing ``_is_prior_fy_close`` tests (the July-default
regression case). See also ``tests/services/test_reports_comparative_helpers.py``
for pure-function coverage of ``reports_svc.fy_bounds_for_company`` and
``merge_comparative_lines``'s account-code re-sort.
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.tenant import Tenant

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/api/v1/test_reports_csv.py + test_reports_financial.py)
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
    """One account per relevant AccountType, scoped to the seed company + tenant."""
    async with AsyncSessionLocal() as session:
        seed_company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
                .limit(1)
            )
        ).scalars().first()
        assert seed_company is not None
        result: dict[str, str] = {}
        for at in (AccountType.INCOME, AccountType.EXPENSE, AccountType.ASSET):
            row = (
                await session.execute(
                    select(Account).where(
                        Account.archived_at.is_(None),
                        Account.account_type == at,
                        Account.is_header.is_(False),
                        Account.tenant_id == DEFAULT_TENANT_ID,
                        Account.company_id == seed_company.id,
                    )
                    .order_by(Account.code)
                    .limit(1)
                )
            ).scalars().first()
            assert row is not None, f"Test DB has no non-header {at.value} account"
            result[at.value] = str(row.id)
    return result


@pytest.fixture
async def tenant_b() -> str:
    """A real second tenant WITH its own Company (mirrors test_reports_csv.py's
    fixture / 6b1bfd3 -- a bare random UUID has no Company row and always
    404s at ``get_active_company_id``, never exercising the isolation
    assertion)."""
    suffix = uuid.uuid4().hex[:8]
    tenant_id = uuid.uuid4()
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name=f"CompTenantB-{suffix}",
                slug=f"comp-tenant-b-{suffix}",
            )
        )
        await session.flush()
        session.add(
            Company(
                id=company_id,
                tenant_id=tenant_id,
                name=f"CompTenantB-{suffix}",
                base_currency="AUD",
                fin_year_start_month=7,
            )
        )
        await session.commit()

    yield str(tenant_id)

    async with AsyncSessionLocal() as session:
        company_row = await session.get(Company, company_id)
        if company_row is not None:
            await session.delete(company_row)
        tenant_row = await session.get(Tenant, tenant_id)
        if tenant_row is not None:
            await session.delete(tenant_row)
        await session.commit()


@pytest.fixture
async def tenant_uk_fy() -> str:
    """A second tenant + Company anchored on the UK financial year (6 April,
    ``fin_year_start_month=4``/``fin_year_start_day=6``) -- Finding 1's
    regression fixture. Proves ``compare=previous_period`` on BS/TB derives
    the comparative date from THIS company's own FY anchor rather than the
    AU-hardcoded 1 July. No GL accounts/JEs are needed for these tests --
    the comparative *date* the fix produces is independent of ledger data
    (mirrors ``tenant_b`` otherwise)."""
    suffix = uuid.uuid4().hex[:8]
    tenant_id = uuid.uuid4()
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name=f"UKFYTenant-{suffix}",
                slug=f"uk-fy-tenant-{suffix}",
            )
        )
        await session.flush()
        session.add(
            Company(
                id=company_id,
                tenant_id=tenant_id,
                name=f"UKFYCompany-{suffix}",
                base_currency="GBP",
                fin_year_start_month=4,
                fin_year_start_day=6,
            )
        )
        await session.commit()

    yield str(tenant_id)

    async with AsyncSessionLocal() as session:
        company_row = await session.get(Company, company_id)
        if company_row is not None:
            await session.delete(company_row)
        tenant_row = await session.get(Tenant, tenant_id)
        if tenant_row is not None:
            await session.delete(tenant_row)
        await session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_and_post_je(client: AsyncClient, entry_date: str, lines: list[dict]) -> dict:
    r = await client.post(
        "/api/v1/journal_entries",
        json={"entry_date": entry_date, "narration": "R7 comparative test entry", "lines": lines},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/journal_entries/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


def _balanced_lines(debit_account_id: str, credit_account_id: str, amount: str) -> list[dict]:
    return [
        {"account_id": debit_account_id, "debit": amount, "credit": "0"},
        {"account_id": credit_account_id, "debit": "0", "credit": amount},
    ]


# ---------------------------------------------------------------------------
# P&L — ?compare=previous_period|previous_year
# ---------------------------------------------------------------------------


async def test_pnl_compare_omitted_shape_unchanged(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """No ``compare`` param -> response has none of the R7 comparative keys.

    Uses a POPULATED income line (not an empty range) -- an empty list
    would make the per-line ``"comparative" not in line`` assertion
    vacuously true and miss a leaked ``"comparative": null``.
    """
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    await _create_and_post_je(
        api_client, "2100-04-15", lines=_balanced_lines(asset_id, income_id, "777.00")
    )

    r = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={"from_date": "2100-04-01", "to_date": "2100-04-30"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "compare" not in body
    assert "net_profit_comparative" not in body
    assert "comparative_from_date" not in body
    assert "comparative_to_date" not in body
    assert "total_income_comparative" not in body["income"]
    assert "total_expenses_comparative" not in body["expenses"]

    income_lines = [
        line for line in body["income"]["INCOME"] if line["account_id"] == income_id
    ]
    assert income_lines, "INCOME account not found in P&L income section"
    assert abs(income_lines[0]["amount"] - 777.0) < 0.01
    assert "comparative" not in income_lines[0]


async def test_pnl_compare_previous_period_math(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """``previous_period`` = the immediately-preceding period of equal length.

    Expected dates are hardcoded independently of the route's own formula
    (``prev_to = from_date - 1 day``; ``prev_from`` shifted back by the
    current period's day-count) -- re-deriving the same arithmetic in the
    test would let a bug in that formula pass silently."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    from_date = date(2101, 4, 1)
    to_date = date(2101, 4, 30)
    # April 1..30 is a 29-day span (2101-04-30 minus 2101-04-01) -> the
    # comparative period is 2101-03-02..2101-03-31 (also 29 days).
    prev_from = date(2101, 3, 2)
    prev_to = date(2101, 3, 31)

    await _create_and_post_je(
        api_client, "2101-04-15", lines=_balanced_lines(asset_id, income_id, "1000.00")
    )
    await _create_and_post_je(
        api_client, prev_from.isoformat(), lines=_balanced_lines(asset_id, income_id, "400.00")
    )

    r = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "compare": "previous_period",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["compare"] == "previous_period"
    assert body["comparative_from_date"] == prev_from.isoformat()
    assert body["comparative_to_date"] == prev_to.isoformat()

    income_lines = [
        line for line in body["income"]["INCOME"] if line["account_id"] == income_id
    ]
    assert income_lines, "INCOME account not found in P&L income section"
    assert abs(income_lines[0]["amount"] - 1000.0) < 0.01
    assert abs(income_lines[0]["comparative"] - 400.0) < 0.01
    assert abs(body["income"]["total_income_comparative"] - 400.0) < 0.01
    assert abs(body["net_profit_comparative"] - 400.0) < 0.01


async def test_pnl_compare_previous_year_math(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """``previous_year`` = the same date range shifted back a leap-safe year."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client, "2103-05-10", lines=_balanced_lines(asset_id, income_id, "1500.00")
    )
    await _create_and_post_je(
        api_client, "2102-05-10", lines=_balanced_lines(asset_id, income_id, "600.00")
    )

    r = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={
            "from_date": "2103-05-01",
            "to_date": "2103-05-31",
            "compare": "previous_year",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["compare"] == "previous_year"
    assert body["comparative_from_date"] == "2102-05-01"
    assert body["comparative_to_date"] == "2102-05-31"

    income_lines = [
        line for line in body["income"]["INCOME"] if line["account_id"] == income_id
    ]
    assert income_lines
    assert abs(income_lines[0]["amount"] - 1500.0) < 0.01
    assert abs(income_lines[0]["comparative"] - 600.0) < 0.01


async def test_pnl_compare_previous_year_leap_day(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """``previous_year`` on a range touching 29 Feb of a leap year --
    ``subtract_one_year`` has no 29 Feb in the non-leap prior year, so both
    bounds fall back to 28 Feb. 2124 is a leap year (divisible by 4, not by
    100); 2123 is not. Expected dates are hardcoded, not re-derived from
    ``subtract_one_year`` itself."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client, "2124-02-15", lines=_balanced_lines(asset_id, income_id, "1200.00")
    )
    await _create_and_post_je(
        api_client, "2123-02-20", lines=_balanced_lines(asset_id, income_id, "300.00")
    )

    r = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={
            "from_date": "2124-02-01",
            "to_date": "2124-02-29",
            "compare": "previous_year",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["compare"] == "previous_year"
    assert body["comparative_from_date"] == "2123-02-01"
    assert body["comparative_to_date"] == "2123-02-28"

    income_lines = [
        line for line in body["income"]["INCOME"] if line["account_id"] == income_id
    ]
    assert income_lines
    assert abs(income_lines[0]["amount"] - 1200.0) < 0.01
    assert abs(income_lines[0]["comparative"] - 300.0) < 0.01


async def test_pnl_compare_invalid_value_422(api_client: AsyncClient) -> None:
    r = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={"from_date": "2104-01-01", "to_date": "2104-01-31", "compare": "last_month"},
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Balance Sheet — ?compare=previous_period|previous_year
#
# The balance sheet is cumulative-from-inception as at a single date, so
# (unlike P&L) two calls at different as_of dates are NOT independent --
# every JE dated on/before BOTH dates counts toward BOTH balances. Assertions
# below use the delta (balance - comparative) rather than an absolute value,
# matching test_reports_financial.py's test_balance_sheet_cumulative pattern.
# ---------------------------------------------------------------------------


async def test_bs_compare_omitted_shape_unchanged(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """No ``compare`` param -> response has none of the R7 comparative keys.

    Uses a POPULATED asset line (not an empty as_of_date) -- an empty list
    would make the per-line ``"comparative" not in line`` assertion
    vacuously true and miss a leaked ``"comparative": null``.
    """
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]
    await _create_and_post_je(
        api_client, "2097-06-01", lines=_balanced_lines(asset_id, income_id, "888.00")
    )

    r = await api_client.get(
        "/api/v1/reports/balance_sheet", params={"as_of_date": "2097-06-30"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "compare" not in body
    assert "comparative_as_of_date" not in body
    assert "total_assets_comparative" not in body["assets"]
    assert "total_liabilities_comparative" not in body["liabilities"]
    assert "total_equity_comparative" not in body["equity"]

    asset_lines = [line for line in body["assets"]["ASSET"] if line["account_id"] == asset_id]
    assert asset_lines, "ASSET account not found in balance sheet"
    assert asset_lines[0]["balance"] > 0
    assert "comparative" not in asset_lines[0]


async def test_bs_compare_previous_year_math(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]

    # Counts in BOTH current (as_of 2110-04-01) and comparative (as_of 2109-04-01).
    await _create_and_post_je(
        api_client, "2109-04-01", lines=_balanced_lines(asset_id, income_id, "3000.00")
    )
    # Dated after the comparative as_of but before current as_of -> current only.
    await _create_and_post_je(
        api_client, "2109-06-01", lines=_balanced_lines(asset_id, income_id, "5000.00")
    )

    r = await api_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "2110-04-01", "compare": "previous_year"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["compare"] == "previous_year"
    assert body["comparative_as_of_date"] == "2109-04-01"

    asset_lines = [line for line in body["assets"]["ASSET"] if line["account_id"] == asset_id]
    assert asset_lines, "ASSET account not found in balance sheet"
    delta = asset_lines[0]["balance"] - asset_lines[0]["comparative"]
    assert abs(delta - 5000.0) < 0.01, (
        f"Expected the June-only JE (5000) to be the sole delta, got {delta}"
    )


async def test_bs_compare_previous_period_is_prior_fy_close(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """R7 convention (no PDF-pack precedent for BS previous_period): the
    comparative as-at date is the CLOSE of the prior financial year
    containing ``as_of_date``, ``fy_start - 1 day``, computed from the
    seed company's own ``fin_year_start_month``/``fin_year_start_day``
    (default 1 Jul / day 1) via ``reports_svc.fy_bounds_for_company`` --
    this is also the July-default regression case for the Finding 1 fix
    (see ``test_bs_compare_previous_period_uk_fy`` for the non-July case)."""
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]

    # as_of 2112-03-15 -> FY 2111-07-01..2112-06-30 -> prior FY close = 2111-06-30.
    # Dated exactly on the comparative boundary -> counts in BOTH.
    await _create_and_post_je(
        api_client, "2111-06-30", lines=_balanced_lines(asset_id, income_id, "2000.00")
    )
    # Dated after the comparative boundary, before current as_of -> current only.
    await _create_and_post_je(
        api_client, "2111-12-01", lines=_balanced_lines(asset_id, income_id, "4500.00")
    )

    r = await api_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "2112-03-15", "compare": "previous_period"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["compare"] == "previous_period"
    assert body["comparative_as_of_date"] == "2111-06-30"

    asset_lines = [line for line in body["assets"]["ASSET"] if line["account_id"] == asset_id]
    assert asset_lines
    delta = asset_lines[0]["balance"] - asset_lines[0]["comparative"]
    assert abs(delta - 4500.0) < 0.01, (
        f"Expected the post-FY-close JE (4500) to be the sole delta, got {delta}"
    )


async def test_bs_compare_previous_period_uk_fy(
    api_client: AsyncClient, tenant_uk_fy: str
) -> None:
    """Finding 1 fix: a company anchored on the UK financial year (6 April,
    NOT 1 July) gets the comparative date computed off ITS OWN FY anchor.
    ``as_of_date=2130-03-15`` falls in the UK FY 2129-04-06..2130-04-05, so
    the prior-FY-close comparative is 2129-04-05 -- the pre-fix
    AU-hardcoded ``_current_fy_bounds`` would instead have produced
    2129-06-30 (1 July FY), a financially wrong comparative with a 200."""
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_uk_fy
    try:
        r = await api_client.get(
            "/api/v1/reports/balance_sheet",
            params={"as_of_date": "2130-03-15", "compare": "previous_period"},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["compare"] == "previous_period"
    assert body["comparative_as_of_date"] == "2129-04-05"


async def test_bs_compare_invalid_value_422(api_client: AsyncClient) -> None:
    r = await api_client.get(
        "/api/v1/reports/balance_sheet",
        params={"as_of_date": "2113-01-01", "compare": "last_month"},
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Trial Balance — ?compare=previous_period|previous_year
# ---------------------------------------------------------------------------


async def test_tb_compare_omitted_shape_unchanged(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """No ``compare`` param -> response has none of the R7 comparative keys.

    Uses a POPULATED account line (not an empty as_of_date) -- an empty
    list would make the per-line ``"comparative" not in line`` assertion
    vacuously true and miss a leaked ``"comparative": null``.
    """
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]
    await _create_and_post_je(
        api_client, "2098-06-01", lines=_balanced_lines(asset_id, income_id, "999.00")
    )

    r = await api_client.get(
        "/api/v1/reports/trial_balance", params={"as_of_date": "2098-06-30"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "compare" not in body
    assert "comparative_as_of_date" not in body
    assert "total_debits_comparative" not in body
    assert "total_credits_comparative" not in body

    asset_lines = [line for line in body["accounts"] if line["account_id"] == asset_id]
    assert asset_lines, "ASSET account not found in trial balance"
    assert asset_lines[0]["balance"] > 0
    assert "comparative" not in asset_lines[0]


async def test_tb_compare_previous_year_math(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]

    # Counts in BOTH current (as_of 2115-04-01) and comparative (as_of 2114-04-01).
    await _create_and_post_je(
        api_client, "2114-04-01", lines=_balanced_lines(asset_id, income_id, "1000.00")
    )
    # Dated after the comparative as_of but before current as_of -> current only.
    await _create_and_post_je(
        api_client, "2114-08-01", lines=_balanced_lines(asset_id, income_id, "2500.00")
    )

    r = await api_client.get(
        "/api/v1/reports/trial_balance",
        params={"as_of_date": "2115-04-01", "compare": "previous_year"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["compare"] == "previous_year"
    assert body["comparative_as_of_date"] == "2114-04-01"

    asset_lines = [line for line in body["accounts"] if line["account_id"] == asset_id]
    assert asset_lines, "ASSET account not found in trial balance"
    delta = asset_lines[0]["balance"] - asset_lines[0]["comparative"]
    assert abs(delta - 2500.0) < 0.01, (
        f"Expected the August-only JE (2500) to be the sole delta, got {delta}"
    )

    total_debits_delta = body["total_debits"] - body["total_debits_comparative"]
    total_credits_delta = body["total_credits"] - body["total_credits_comparative"]
    assert total_debits_delta >= 2500.0 - 0.01
    assert total_credits_delta >= 2500.0 - 0.01


async def test_tb_compare_previous_period_is_prior_fy_close(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """Same R7 convention as the balance sheet's ``previous_period`` --
    the prior financial-year close, ``fy_start - 1 day`` -- computed from
    the seed company's own ``fin_year_start_month``/``fin_year_start_day``
    (default 1 Jul / day 1) via ``reports_svc.fy_bounds_for_company``. This
    is the July-default regression case for the Finding 1 fix on the
    trial-balance route (see ``test_tb_compare_previous_period_uk_fy`` for
    the non-July case)."""
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]

    # as_of 2120-03-15 -> FY 2119-07-01..2120-06-30 -> prior FY close = 2119-06-30.
    # Dated exactly on the comparative boundary -> counts in BOTH.
    await _create_and_post_je(
        api_client, "2119-06-30", lines=_balanced_lines(asset_id, income_id, "2000.00")
    )
    # Dated after the comparative boundary, before current as_of -> current only.
    await _create_and_post_je(
        api_client, "2119-12-01", lines=_balanced_lines(asset_id, income_id, "4500.00")
    )

    r = await api_client.get(
        "/api/v1/reports/trial_balance",
        params={"as_of_date": "2120-03-15", "compare": "previous_period"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["compare"] == "previous_period"
    assert body["comparative_as_of_date"] == "2119-06-30"

    asset_lines = [line for line in body["accounts"] if line["account_id"] == asset_id]
    assert asset_lines
    delta = asset_lines[0]["balance"] - asset_lines[0]["comparative"]
    assert abs(delta - 4500.0) < 0.01, (
        f"Expected the post-FY-close JE (4500) to be the sole delta, got {delta}"
    )

    total_debits_delta = body["total_debits"] - body["total_debits_comparative"]
    total_credits_delta = body["total_credits"] - body["total_credits_comparative"]
    assert total_debits_delta >= 4500.0 - 0.01
    assert total_credits_delta >= 4500.0 - 0.01


async def test_tb_compare_previous_period_uk_fy(
    api_client: AsyncClient, tenant_uk_fy: str
) -> None:
    """Finding 1 fix, trial-balance route: same UK FY anchor (6 April) as
    ``test_bs_compare_previous_period_uk_fy`` -- the comparative date must
    come from THIS company's own FY anchor, not the AU-hardcoded 1 July."""
    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_uk_fy
    try:
        r = await api_client.get(
            "/api/v1/reports/trial_balance",
            params={"as_of_date": "2130-03-15", "compare": "previous_period"},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["compare"] == "previous_period"
    assert body["comparative_as_of_date"] == "2129-04-05"


async def test_tb_compare_invalid_value_422(api_client: AsyncClient) -> None:
    r = await api_client.get(
        "/api/v1/reports/trial_balance",
        params={"as_of_date": "2116-01-01", "compare": "last_month"},
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Tenant isolation with ``compare`` enabled
# ---------------------------------------------------------------------------


async def test_pnl_compare_tenant_isolation(
    api_client: AsyncClient, gl_accounts: dict[str, str], tenant_b: str
) -> None:
    """Tenant B's comparative P&L must not see tenant A's GL lines either."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    # Tenant A: current period + comparative-year period both have income.
    await _create_and_post_je(
        api_client, "2117-04-10", lines=_balanced_lines(asset_id, income_id, "1234.00")
    )
    await _create_and_post_je(
        api_client, "2116-04-10", lines=_balanced_lines(asset_id, income_id, "555.00")
    )

    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b
    try:
        r = await api_client.get(
            "/api/v1/reports/profit_loss",
            params={
                "from_date": "2117-04-01",
                "to_date": "2117-04-30",
                "compare": "previous_year",
            },
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["income"]["total_income"] == 0.0
    assert body["income"]["total_income_comparative"] == 0.0


async def test_bs_compare_tenant_isolation(
    api_client: AsyncClient, gl_accounts: dict[str, str], tenant_b: str
) -> None:
    """Tenant B's comparative balance sheet must not see tenant A's GL lines."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client, "2119-01-01", lines=_balanced_lines(asset_id, income_id, "9000.00")
    )

    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b
    try:
        r = await api_client.get(
            "/api/v1/reports/balance_sheet",
            params={"as_of_date": "2119-06-30", "compare": "previous_year"},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assets"]["total_assets"] == 0.0
    assert body["assets"]["total_assets_comparative"] == 0.0


async def test_tb_compare_tenant_isolation(
    api_client: AsyncClient, gl_accounts: dict[str, str], tenant_b: str
) -> None:
    """Tenant B's comparative trial balance must not see tenant A's GL lines
    (mirrors ``test_pnl_compare_tenant_isolation``/``test_bs_compare_tenant_isolation``)."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client, "2122-01-01", lines=_balanced_lines(asset_id, income_id, "9000.00")
    )

    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b
    try:
        r = await api_client.get(
            "/api/v1/reports/trial_balance",
            params={"as_of_date": "2122-06-30", "compare": "previous_year"},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_debits"] == 0.0
    assert body["total_debits_comparative"] == 0.0
    assert body["total_credits"] == 0.0
    assert body["total_credits_comparative"] == 0.0
