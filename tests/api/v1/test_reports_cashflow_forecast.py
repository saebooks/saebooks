"""Tier-5 report tests — /api/v1/reports/cashflow_forecast (audit item R2).

7 tests:
* test_cashflow_forecast_empty
* test_cashflow_forecast_invoice_and_bill_items
* test_cashflow_forecast_projected_closing_formula
* test_cashflow_forecast_weeks_non_empty
* test_cashflow_forecast_horizon_validation
* test_cashflow_forecast_as_of_default_today
* test_cashflow_forecast_recurring_template_is_gst_inclusive
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tax_code import TaxCode

pytestmark = pytest.mark.postgres_only


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
async def forecast_deps() -> dict[str, str]:
    """Fresh contact + income/expense accounts, and a fresh contact per
    test (mirrors test_reports_aged.py) so prior tests' open invoices/
    bills don't leak into the forecast horizon.
    """
    async with AsyncSessionLocal() as session:
        seed_company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
                .limit(1)
            )
        ).scalars().first()
        assert seed_company is not None, "Test DB has no seeded company"

        income = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                    Account.company_id == seed_company.id,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
                .limit(1)
            )
        ).scalars().first()
        assert income is not None, "Test DB has no INCOME account"

        expense = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                    Account.company_id == seed_company.id,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
                .limit(1)
            )
        ).scalars().first()
        assert expense is not None, "Test DB has no EXPENSE account"

        contact = Contact(
            tenant_id=DEFAULT_TENANT_ID,
            company_id=seed_company.id,
            name=f"ForecastTest-{uuid.uuid4().hex[:8]}",
            contact_type=ContactType.BOTH,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)

    return {
        "income_account_id": str(income.id),
        "expense_account_id": str(expense.id),
        "contact_id": str(contact.id),
    }


@pytest.fixture
async def gst_tax_code() -> dict[str, str]:
    """The seeded AU 'GST' tax code for the seed company (rate + id).

    Scoped to the seed company + DEFAULT_TENANT_ID, mirroring
    ``tax_codes`` in test_reports_bas_cashflow.py.
    """
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
        gst_row = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.archived_at.is_(None),
                    TaxCode.code == "GST",
                    TaxCode.tenant_id == DEFAULT_TENANT_ID,
                    TaxCode.company_id == seed_company.id,
                )
            )
        ).scalars().first()
        assert gst_row is not None, "Seed tax code GST not found"
        return {"id": str(gst_row.id), "rate": str(gst_row.rate)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_and_post_invoice(
    client: AsyncClient,
    deps: dict[str, str],
    issue_date: str,
    due_date: str,
    amount: str = "1500.00",
) -> dict:
    r = await client.post(
        "/api/v1/invoices",
        json={
            "contact_id": deps["contact_id"],
            "issue_date": issue_date,
            "due_date": due_date,
            "lines": [
                {
                    "description": "Cashflow forecast test invoice",
                    "account_id": deps["income_account_id"],
                    "quantity": "1",
                    "unit_price": amount,
                    "discount_pct": "0",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/invoices/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


async def _create_and_post_bill(
    client: AsyncClient,
    deps: dict[str, str],
    issue_date: str,
    due_date: str,
    amount: str = "600.00",
) -> dict:
    r = await client.post(
        "/api/v1/bills",
        json={
            "contact_id": deps["contact_id"],
            "issue_date": issue_date,
            "due_date": due_date,
            "lines": [
                {
                    "description": "Cashflow forecast test bill",
                    "account_id": deps["expense_account_id"],
                    "quantity": "1",
                    "unit_price": amount,
                    "discount_pct": "0",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/bills/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_cashflow_forecast_empty(api_client: AsyncClient) -> None:
    """No open invoices/bills for a fresh contact-less window still 200s with a valid shape."""
    r = await api_client.get(
        "/api/v1/reports/cashflow_forecast",
        params={"horizon_days": 7, "as_of": "1999-01-01"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from_date"] == "1999-01-01"
    assert isinstance(body["items"], list)
    assert isinstance(body["weeks"], list)
    assert len(body["weeks"]) > 0


async def test_cashflow_forecast_invoice_and_bill_items(
    api_client: AsyncClient, forecast_deps: dict[str, str]
) -> None:
    """A posted unpaid invoice and bill within the horizon appear as signed items."""
    today = date.today()
    due_in_10 = today + timedelta(days=10)
    due_in_20 = today + timedelta(days=20)

    posted_invoice = await _create_and_post_invoice(
        api_client, forecast_deps, issue_date=today.isoformat(),
        due_date=due_in_10.isoformat(), amount="1500.00",
    )
    posted_bill = await _create_and_post_bill(
        api_client, forecast_deps, issue_date=today.isoformat(),
        due_date=due_in_20.isoformat(), amount="600.00",
    )

    r = await api_client.get(
        "/api/v1/reports/cashflow_forecast",
        params={"horizon_days": 90, "as_of": today.isoformat()},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    invoice_items = [
        i for i in body["items"]
        if i["source"] == "invoice" and i["source_id"] == posted_invoice["id"]
    ]
    assert len(invoice_items) == 1, "Expected exactly one forecast item for the posted invoice"
    # Compare against the posted document's own total (not the 1500.00 line
    # price) -- the service item amount is inv.total - inv.amount_paid,
    # which includes GST if the invoice's tax code adds any.
    assert invoice_items[0]["amount"] == pytest.approx(
        float(posted_invoice["total"]), abs=0.01
    ), "Invoice forecast item should be a positive inflow equal to the invoice total"

    bill_items = [
        i for i in body["items"]
        if i["source"] == "bill" and i["source_id"] == posted_bill["id"]
    ]
    assert len(bill_items) == 1, "Expected exactly one forecast item for the posted bill"
    assert bill_items[0]["amount"] == pytest.approx(
        -float(posted_bill["total"]), abs=0.01
    ), "Bill forecast item should be a negative outflow equal to the bill total"


async def test_cashflow_forecast_projected_closing_formula(
    api_client: AsyncClient, forecast_deps: dict[str, str]
) -> None:
    """projected_closing == opening_balance + total_inflows - total_outflows."""
    today = date.today()
    due_in_5 = today + timedelta(days=5)

    await _create_and_post_invoice(
        api_client, forecast_deps, issue_date=today.isoformat(),
        due_date=due_in_5.isoformat(), amount="2000.00",
    )
    await _create_and_post_bill(
        api_client, forecast_deps, issue_date=today.isoformat(),
        due_date=due_in_5.isoformat(), amount="750.00",
    )

    r = await api_client.get(
        "/api/v1/reports/cashflow_forecast",
        params={"horizon_days": 30, "as_of": today.isoformat()},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    expected_closing = body["opening_balance"] + body["total_inflows"] - body["total_outflows"]
    assert body["projected_closing"] == pytest.approx(expected_closing, abs=0.01)
    assert body["total_inflows"] >= 2000.0
    assert body["total_outflows"] >= 750.0


async def test_cashflow_forecast_weeks_non_empty(
    api_client: AsyncClient, forecast_deps: dict[str, str]
) -> None:
    """The weekly roll-up covers the horizon and running_balance accumulates."""
    today = date.today()
    r = await api_client.get(
        "/api/v1/reports/cashflow_forecast",
        params={"horizon_days": 28, "as_of": today.isoformat()},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert len(body["weeks"]) >= 4  # 28 days / 7 == 4 weekly buckets (inclusive of horizon end)
    first_week = body["weeks"][0]
    assert first_week["start"] == today.isoformat()
    for key in ("start", "inflows", "outflows", "net", "running_balance"):
        assert key in first_week


async def test_cashflow_forecast_horizon_validation(api_client: AsyncClient) -> None:
    """horizon_days outside [7, 365] is rejected with 422."""
    r = await api_client.get(
        "/api/v1/reports/cashflow_forecast",
        params={"horizon_days": 5000},
    )
    assert r.status_code == 422, r.text

    r2 = await api_client.get(
        "/api/v1/reports/cashflow_forecast",
        params={"horizon_days": 1},
    )
    assert r2.status_code == 422, r2.text


async def test_cashflow_forecast_as_of_default_today(api_client: AsyncClient) -> None:
    """Omitting ``as_of`` defaults to today."""
    r = await api_client.get("/api/v1/reports/cashflow_forecast")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from_date"] == date.today().isoformat()


async def test_cashflow_forecast_recurring_template_is_gst_inclusive(
    api_client: AsyncClient,
    forecast_deps: dict[str, str],
    gst_tax_code: dict[str, str],
) -> None:
    """An ACTIVE recurring template with a GST-coded line projects a
    GST-INCLUSIVE forecast item, matching what ``materialise_one`` would
    actually invoice -- not the bare ex-GST line subtotal.
    """
    today = date.today()

    r = await api_client.post(
        "/api/v1/recurring_invoices",
        json={
            "name": f"Forecast GST Retainer {uuid.uuid4().hex[:8]}",
            "contact_id": forecast_deps["contact_id"],
            "frequency": "MONTHLY",
            "next_run": today.isoformat(),
            "lines": [
                {
                    "description": "GST-coded retainer line",
                    "account_id": forecast_deps["income_account_id"],
                    "tax_code_id": gst_tax_code["id"],
                    "quantity": "1",
                    "unit_price": "1000.00",
                    "discount_pct": "0",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    template_id = r.json()["id"]

    rate = Decimal(gst_tax_code["rate"])
    expected_total = (Decimal("1000.00") * (Decimal("1") + rate / Decimal("100"))).quantize(
        Decimal("0.01")
    )
    # Sanity: the seeded GST code is a real >0% rate, so an ex-GST bug
    # (amount == 1000.00 exactly) would be distinguishable from the fix.
    assert expected_total > Decimal("1000.00"), "Seed GST tax code has a 0% rate?"

    r2 = await api_client.get(
        "/api/v1/reports/cashflow_forecast",
        params={"horizon_days": 7, "as_of": today.isoformat()},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()

    recurring_items = [
        i for i in body["items"]
        if i["source"] == "recurring" and i["source_id"] == template_id
    ]
    assert len(recurring_items) == 1, (
        "Expected exactly one forecast item for the recurring template"
    )
    assert recurring_items[0]["amount"] == pytest.approx(float(expected_total), abs=0.01), (
        "Recurring forecast item should be GST-inclusive, matching the "
        "invoice/bill branches (both use doc.total, which includes GST)"
    )
