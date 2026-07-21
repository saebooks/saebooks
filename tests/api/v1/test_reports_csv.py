"""Tier-5 report tests — CSV export routes (audit item R1).

Covers: /aged_receivables.csv, /aged_payables.csv, /trial_balance.csv,
/profit_loss.csv, /balance_sheet.csv.

11 tests:
* test_aged_receivables_csv_headers_and_row
* test_aged_receivables_csv_content_disposition
* test_aged_receivables_csv_tenant_isolation
* test_aged_payables_csv_headers_and_row
* test_trial_balance_csv_headers_and_row
* test_trial_balance_csv_matches_json
* test_profit_loss_csv_headers_and_rows
* test_profit_loss_csv_matches_json
* test_balance_sheet_csv_headers_and_rows
* test_balance_sheet_csv_matches_json
* test_aged_receivables_csv_formula_injection_guard
"""
from __future__ import annotations

import csv
import io
import os
import uuid
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.tenant import Tenant

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
async def invoice_deps() -> dict[str, str]:
    """Fresh contact + income account for a clean AR slate (mirrors test_reports_aged.py)."""
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

        contact = Contact(
            tenant_id=DEFAULT_TENANT_ID,
            company_id=seed_company.id,
            name=f"CsvAgedTest-{uuid.uuid4().hex[:8]}",
            contact_type=ContactType.BOTH,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)

    return {
        "income_account_id": str(income.id),
        "contact_id": str(contact.id),
    }


@pytest.fixture
async def bill_deps() -> dict[str, str]:
    """Fresh contact + expense account for a clean AP slate."""
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
            name=f"CsvAgedBillTest-{uuid.uuid4().hex[:8]}",
            contact_type=ContactType.BOTH,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)

    return {
        "expense_account_id": str(expense.id),
        "contact_id": str(contact.id),
    }


@pytest.fixture
async def tenant_b() -> str:
    """A real second tenant WITH its own Company (mirrors the seeding
    pattern in tests/test_rls_company_jurisdictions.py). Unlike pointing
    ``SAEBOOKS_DEV_TENANT_ID`` at a bare random UUID (which has no
    Company row and always 404s at ``get_active_company_id``, making the
    isolation assertion dead code), this lets the request actually reach
    200 so the "tenant A's data is absent" assertion is exercised.
    """
    suffix = uuid.uuid4().hex[:8]
    tenant_id = uuid.uuid4()
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name=f"CsvTenantB-{suffix}",
                slug=f"csv-tenant-b-{suffix}",
            )
        )
        await session.flush()
        session.add(
            Company(
                id=company_id,
                tenant_id=tenant_id,
                name=f"CsvTenantB-{suffix}",
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoice_payload(
    deps: dict[str, str], issue_date: str, due_date: str, amount: str = "1000.00"
) -> dict:
    return {
        "contact_id": deps["contact_id"],
        "issue_date": issue_date,
        "due_date": due_date,
        "lines": [
            {
                "description": "CSV aged AR test line",
                "account_id": deps["income_account_id"],
                "quantity": "1",
                "unit_price": amount,
                "discount_pct": "0",
            }
        ],
    }


def _bill_payload(
    deps: dict[str, str], issue_date: str, due_date: str, amount: str = "800.00"
) -> dict:
    return {
        "contact_id": deps["contact_id"],
        "issue_date": issue_date,
        "due_date": due_date,
        "lines": [
            {
                "description": "CSV aged AP test line",
                "account_id": deps["expense_account_id"],
                "quantity": "1",
                "unit_price": amount,
                "discount_pct": "0",
            }
        ],
    }


async def _create_and_post_invoice(
    client: AsyncClient, deps: dict[str, str], issue_date: str, due_date: str
) -> dict:
    r = await client.post("/api/v1/invoices", json=_invoice_payload(deps, issue_date, due_date))
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/invoices/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


async def _create_and_post_bill(
    client: AsyncClient, deps: dict[str, str], issue_date: str, due_date: str
) -> dict:
    r = await client.post("/api/v1/bills", json=_bill_payload(deps, issue_date, due_date))
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/bills/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


async def _create_and_post_je(client: AsyncClient, entry_date: str, lines: list[dict]) -> dict:
    r = await client.post(
        "/api/v1/journal_entries",
        json={"entry_date": entry_date, "narration": "CSV report test entry", "lines": lines},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/journal_entries/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


def _rows(csv_text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_text)))


# ---------------------------------------------------------------------------
# Aged Receivables / Payables CSV
# ---------------------------------------------------------------------------


async def test_aged_receivables_csv_headers_and_row(
    api_client: AsyncClient, invoice_deps: dict[str, str]
) -> None:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    posted = await _create_and_post_invoice(
        api_client, invoice_deps, issue_date=today.isoformat(), due_date=tomorrow.isoformat()
    )

    r = await api_client.get(
        "/api/v1/reports/aged_receivables.csv",
        params={"as_of_date": today.isoformat()},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/csv; charset=utf-8"

    rows = _rows(r.text)
    assert rows[0] == [
        "contact",
        "invoice_number",
        "issue_date",
        "due_date",
        "total",
        "paid",
        "balance_due",
        "days_overdue",
        "bucket",
    ]
    numbers = [row[1] for row in rows[1:]]
    assert posted["number"] in numbers, "Posted invoice not present in aged_receivables.csv"


async def test_aged_receivables_csv_content_disposition(
    api_client: AsyncClient,
) -> None:
    r = await api_client.get(
        "/api/v1/reports/aged_receivables.csv",
        params={"as_of_date": "2000-01-01"},
    )
    assert r.status_code == 200, r.text
    disposition = r.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "aged_receivables_2000-01-01.csv" in disposition


async def test_aged_receivables_csv_tenant_isolation(
    api_client: AsyncClient, invoice_deps: dict[str, str], tenant_b: str
) -> None:
    """Tenant B's aged_receivables.csv does not include tenant A's invoice.

    ``tenant_b`` seeds a REAL tenant with its own Company (unlike a bare
    random UUID with no Company row, which always 404s at
    ``get_active_company_id`` and never actually exercises the isolation
    assertion) so this test requires and gets a 200 from tenant B's own
    company.
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)
    posted = await _create_and_post_invoice(
        api_client, invoice_deps, issue_date=today.isoformat(), due_date=tomorrow.isoformat()
    )

    original = os.environ.get("SAEBOOKS_DEV_TENANT_ID", "")
    os.environ["SAEBOOKS_DEV_TENANT_ID"] = tenant_b
    try:
        r = await api_client.get(
            "/api/v1/reports/aged_receivables.csv",
            params={"as_of_date": today.isoformat()},
        )
    finally:
        if original:
            os.environ["SAEBOOKS_DEV_TENANT_ID"] = original
        else:
            os.environ.pop("SAEBOOKS_DEV_TENANT_ID", None)

    assert r.status_code == 200, r.text
    rows = _rows(r.text)
    numbers = [row[1] for row in rows[1:]]
    assert posted["number"] not in numbers, (
        "Tenant B should not see tenant A's invoice in aged_receivables.csv"
    )


async def test_aged_payables_csv_headers_and_row(
    api_client: AsyncClient, bill_deps: dict[str, str]
) -> None:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    posted = await _create_and_post_bill(
        api_client, bill_deps, issue_date=today.isoformat(), due_date=tomorrow.isoformat()
    )

    r = await api_client.get(
        "/api/v1/reports/aged_payables.csv",
        params={"as_of_date": today.isoformat()},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    disposition = r.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert f"aged_payables_{today.isoformat()}.csv" in disposition

    rows = _rows(r.text)
    assert rows[0] == [
        "contact",
        "bill_number",
        "issue_date",
        "due_date",
        "total",
        "paid",
        "balance_due",
        "days_overdue",
        "bucket",
    ]
    numbers = [row[1] for row in rows[1:]]
    assert posted["number"] in numbers, "Posted bill not present in aged_payables.csv"


# ---------------------------------------------------------------------------
# Trial Balance CSV
# ---------------------------------------------------------------------------


async def test_trial_balance_csv_headers_and_row(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client,
        "2028-01-10",
        lines=[
            {"account_id": asset_id, "debit": "1234.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "1234.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/trial_balance.csv",
        params={"as_of_date": "2028-01-31"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    disposition = r.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "trial_balance_2028-01-31.csv" in disposition

    rows = _rows(r.text)
    assert rows[0] == ["code", "account_name", "account_type", "debit", "credit"]
    assert len(rows) > 1, "Expected at least one account row"
    for row in rows[1:]:
        _code, _name, _acc_type, debit, credit = row
        # Just confirm numeric formatting parses as a two-decimal amount.
        float(debit)
        float(credit)


async def test_trial_balance_csv_matches_json(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """The refactor into a shared assembly helper must not change JSON output."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client,
        "2028-02-10",
        lines=[
            {"account_id": asset_id, "debit": "2468.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "2468.00"},
        ],
    )

    r_json = await api_client.get(
        "/api/v1/reports/trial_balance", params={"as_of_date": "2028-02-28"}
    )
    assert r_json.status_code == 200, r_json.text
    body = r_json.json()

    asset_lines = [a for a in body["accounts"] if a["account_id"] == asset_id]
    assert asset_lines, "ASSET account not found in trial balance JSON"
    assert asset_lines[0]["debit_total"] >= 2468.0

    r_csv = await api_client.get(
        "/api/v1/reports/trial_balance.csv", params={"as_of_date": "2028-02-28"}
    )
    assert r_csv.status_code == 200, r_csv.text
    rows = _rows(r_csv.text)
    codes_in_csv = {row[0] for row in rows[1:]}
    codes_in_json = {a["code"] for a in body["accounts"]}
    assert codes_in_csv == codes_in_json, "CSV account codes should match JSON accounts"


# ---------------------------------------------------------------------------
# Profit & Loss CSV
# ---------------------------------------------------------------------------


async def test_profit_loss_csv_headers_and_rows(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    income_id = gl_accounts[AccountType.INCOME.value]
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client,
        "2028-03-05",
        lines=[
            {"account_id": asset_id, "debit": "3000.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "3000.00"},
        ],
    )
    await _create_and_post_je(
        api_client,
        "2028-03-06",
        lines=[
            {"account_id": expense_id, "debit": "1200.00", "credit": "0"},
            {"account_id": asset_id, "debit": "0", "credit": "1200.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/profit_loss.csv",
        params={"from_date": "2028-03-01", "to_date": "2028-03-31"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    disposition = r.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "profit_loss_2028-03-31.csv" in disposition

    rows = _rows(r.text)
    assert rows[0] == ["section", "code", "account_name", "amount"]
    sections = {row[0] for row in rows[1:]}
    assert sections <= {"income", "expenses"}
    assert "income" in sections
    assert "expenses" in sections


async def test_profit_loss_csv_matches_json(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """The refactor into a shared assembly helper must not change JSON output."""
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]

    await _create_and_post_je(
        api_client,
        "2028-04-05",
        lines=[
            {"account_id": asset_id, "debit": "4321.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "4321.00"},
        ],
    )

    r_json = await api_client.get(
        "/api/v1/reports/profit_loss",
        params={"from_date": "2028-04-01", "to_date": "2028-04-30"},
    )
    assert r_json.status_code == 200, r_json.text
    body = r_json.json()
    income_lines = body["income"]["INCOME"]
    assert any(line["account_id"] == income_id for line in income_lines)

    r_csv = await api_client.get(
        "/api/v1/reports/profit_loss.csv",
        params={"from_date": "2028-04-01", "to_date": "2028-04-30"},
    )
    assert r_csv.status_code == 200, r_csv.text
    rows = _rows(r_csv.text)
    income_codes_csv = {row[1] for row in rows[1:] if row[0] == "income"}
    income_codes_json = {
        line["code"]
        for line in (*body["income"]["INCOME"], *body["income"]["OTHER_INCOME"])
    }
    assert income_codes_csv == income_codes_json


# ---------------------------------------------------------------------------
# Balance Sheet CSV
# ---------------------------------------------------------------------------


async def test_balance_sheet_csv_headers_and_rows(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]

    await _create_and_post_je(
        api_client,
        "2028-05-01",
        lines=[
            {"account_id": asset_id, "debit": "5000.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "5000.00"},
        ],
    )

    r = await api_client.get(
        "/api/v1/reports/balance_sheet.csv",
        params={"as_of_date": "2028-05-31"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    disposition = r.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "balance_sheet_2028-05-31.csv" in disposition

    rows = _rows(r.text)
    assert rows[0] == ["section", "code", "account_name", "balance"]
    sections = {row[0] for row in rows[1:]}
    assert sections <= {"assets", "liabilities", "equity"}
    assert "assets" in sections
    # Synthetic Current Year Earnings row is always present in equity.
    cye_rows = [row for row in rows[1:] if row[0] == "equity" and row[1] == "CYE"]
    assert cye_rows, "Expected the synthetic Current Year Earnings row in equity"


async def test_balance_sheet_csv_matches_json(
    api_client: AsyncClient, gl_accounts: dict[str, str]
) -> None:
    """The refactor into a shared assembly helper must not change JSON output."""
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]

    await _create_and_post_je(
        api_client,
        "2028-06-01",
        lines=[
            {"account_id": asset_id, "debit": "6543.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "6543.00"},
        ],
    )

    r_json = await api_client.get(
        "/api/v1/reports/balance_sheet", params={"as_of_date": "2028-06-30"}
    )
    assert r_json.status_code == 200, r_json.text
    body = r_json.json()
    asset_lines = [a for a in body["assets"]["ASSET"] if a["account_id"] == asset_id]
    assert asset_lines
    assert asset_lines[0]["balance"] > 0

    r_csv = await api_client.get(
        "/api/v1/reports/balance_sheet.csv", params={"as_of_date": "2028-06-30"}
    )
    assert r_csv.status_code == 200, r_csv.text
    rows = _rows(r_csv.text)
    asset_codes_csv = {row[1] for row in rows[1:] if row[0] == "assets"}
    asset_codes_json = {a["code"] for a in body["assets"]["ASSET"]}
    assert asset_codes_csv == asset_codes_json


# ---------------------------------------------------------------------------
# CSV formula-injection guard
# ---------------------------------------------------------------------------


async def test_aged_receivables_csv_formula_injection_guard(
    api_client: AsyncClient,
) -> None:
    """A contact name starting with '=' comes out quote-prefixed in the CSV
    so opening the export in Excel/Sheets/LibreOffice does not evaluate it
    as a formula (CWE-1236 formula-injection guard).
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
        assert income is not None

        contact = Contact(
            tenant_id=DEFAULT_TENANT_ID,
            company_id=seed_company.id,
            name="=SUM(A1:A9)",
            contact_type=ContactType.BOTH,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)

    deps = {"income_account_id": str(income.id), "contact_id": str(contact.id)}
    today = date.today()
    tomorrow = today + timedelta(days=1)
    await _create_and_post_invoice(
        api_client, deps, issue_date=today.isoformat(), due_date=tomorrow.isoformat()
    )

    r = await api_client.get(
        "/api/v1/reports/aged_receivables.csv",
        params={"as_of_date": today.isoformat()},
    )
    assert r.status_code == 200, r.text
    rows = _rows(r.text)
    contact_cells = [row[0] for row in rows[1:]]
    assert "'=SUM(A1:A9)" in contact_cells, (
        "Contact name starting with '=' should be quote-prefixed in the CSV"
    )
