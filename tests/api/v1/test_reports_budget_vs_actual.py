"""Tier-5 report tests — /api/v1/reports/budget_vs_actual (cycle 27).

4 tests:
* test_budget_vs_actual_empty_year
* test_budget_vs_actual_budget_only_line
* test_budget_vs_actual_actual_only_line
* test_budget_vs_actual_year_month_filter
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.budget import Budget
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus


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
async def expense_account_id() -> str:
    """Return an EXPENSE account ID from seeded data."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.is_header.is_(False),
                ).limit(1)
            )
        ).scalars().first()
    assert row is not None, "Test DB has no non-header EXPENSE account"
    return str(row.id)


@pytest.fixture
async def income_account_id() -> str:
    """Return an INCOME account ID from seeded data."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.is_header.is_(False),
                ).limit(1)
            )
        ).scalars().first()
    assert row is not None, "Test DB has no non-header INCOME account"
    return str(row.id)


@pytest.fixture
async def asset_account_id() -> str:
    """Return an ASSET account ID from seeded data."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.ASSET,
                    Account.is_header.is_(False),
                ).limit(1)
            )
        ).scalars().first()
    assert row is not None, "Test DB has no non-header ASSET account"
    return str(row.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_budget(account_id: str, year: int, month: int, amount: str) -> str:
    """Insert a Budget row directly and return its id."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None

        # Resolve tenant_id from the company row
        from saebooks.api.v1.auth import resolve_tenant_id
        tenant_id = resolve_tenant_id()

        budget = Budget(
            id=uuid.uuid4(),
            company_id=company.id,
            tenant_id=tenant_id,
            account_id=uuid.UUID(account_id),
            year=year,
            month=month,
            amount=Decimal(amount),
        )
        session.add(budget)
        await session.commit()
        return str(budget.id)


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
            "narration": "Budget vs actual test entry",
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


async def test_budget_vs_actual_empty_year(api_client: AsyncClient) -> None:
    """A year with no budgets and no GL activity returns empty lines.

    Uses year 2089 which has no seed data or prior test data.
    """
    r = await api_client.get(
        "/api/v1/reports/budget_vs_actual",
        params={"year": 2089},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["year"] == 2089
    assert body["month"] is None
    assert body["lines"] == []
    assert body["total_budget"] == 0.0
    assert body["total_actual"] == 0.0
    assert body["total_variance"] == 0.0


async def test_budget_vs_actual_budget_only_line(
    api_client: AsyncClient,
    expense_account_id: str,
) -> None:
    """Account with budget but no GL activity in the year → actual=0, variance negative.

    Uses year 2090 which has no seed data or prior test data.
    """
    year = 2090
    month = 3
    await _create_budget(expense_account_id, year, month, "4000.00")

    r = await api_client.get(
        "/api/v1/reports/budget_vs_actual",
        params={"year": year},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    matching = [l for l in body["lines"] if l["account_id"] == expense_account_id]
    assert matching, "Budget-only account not found in response"

    line = matching[0]
    # Year 2090 is clean — only the row we just inserted contributes
    assert line["budget"] == pytest.approx(4000.0)
    assert line["actual"] == 0.0
    assert line["variance"] == pytest.approx(-4000.0)
    assert body["total_budget"] >= 4000.0


async def test_budget_vs_actual_actual_only_line(
    api_client: AsyncClient,
    income_account_id: str,
    asset_account_id: str,
) -> None:
    """Account with GL activity but no budget → budget=0, actual>0, variance=actual.

    Uses year 2091 which has no seed data or prior test data.
    """
    year = 2091
    # No budget rows for year 2091.
    await _create_and_post_je(
        api_client,
        f"{year}-06-15",
        lines=[
            {"account_id": asset_account_id, "debit": "9500.00", "credit": "0"},
            {"account_id": income_account_id, "debit": "0", "credit": "9500.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/budget_vs_actual",
        params={"year": year},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    matching = [l for l in body["lines"] if l["account_id"] == income_account_id]
    assert matching, "Actual-only INCOME account not found in response"

    line = matching[0]
    assert line["budget"] == 0.0
    assert line["actual"] >= 9500.0
    # variance = actual - budget
    assert abs(line["variance"] - line["actual"]) < 0.01


async def test_budget_vs_actual_year_month_filter(
    api_client: AsyncClient,
    expense_account_id: str,
) -> None:
    """Month filter returns only that month's budget; other months not included.

    Uses year 2092 which has no seed data or prior test data.
    """
    year = 2092
    # Budget Jan only
    await _create_budget(expense_account_id, year, 1, "1200.00")
    # Budget Jul only
    await _create_budget(expense_account_id, year, 7, "800.00")

    # Full-year query: both months summed
    r_year = await api_client.get(
        "/api/v1/reports/budget_vs_actual",
        params={"year": year},
    )
    assert r_year.status_code == 200, r_year.text
    body_year = r_year.json()
    year_matching = [l for l in body_year["lines"] if l["account_id"] == expense_account_id]
    assert year_matching, "Account not found in year query"
    assert year_matching[0]["budget"] == pytest.approx(2000.0)

    # Month=1 query: only Jan budget
    r_jan = await api_client.get(
        "/api/v1/reports/budget_vs_actual",
        params={"year": year, "month": 1},
    )
    assert r_jan.status_code == 200, r_jan.text
    body_jan = r_jan.json()
    assert body_jan["month"] == 1
    jan_matching = [l for l in body_jan["lines"] if l["account_id"] == expense_account_id]
    assert jan_matching, "Account not found in month=1 query"
    assert jan_matching[0]["budget"] == pytest.approx(1200.0)

    # Month=7 query: only Jul budget
    r_jul = await api_client.get(
        "/api/v1/reports/budget_vs_actual",
        params={"year": year, "month": 7},
    )
    assert r_jul.status_code == 200, r_jul.text
    body_jul = r_jul.json()
    assert body_jul["month"] == 7
    jul_matching = [l for l in body_jul["lines"] if l["account_id"] == expense_account_id]
    assert jul_matching, "Account not found in month=7 query"
    assert jul_matching[0]["budget"] == pytest.approx(800.0)
