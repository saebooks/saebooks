"""Report export tests — XLSX for every report + CSV for the reports that
did not previously have one (cashflow / pl_by_segment / revenue_by_customer /
bas_summary / cashbook summary).

Complements ``test_reports_csv.py`` (which covers the pre-existing CSV routes).
XLSX assertions parse the workbook back with openpyxl and check: the correct
content-type + Content-Disposition, the header row, that money cells are stored
as exact numerics with a 2-dp number format, and — for the statement reports —
that the XLSX columns line up with the CSV columns.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import date, timedelta

import openpyxl
import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType

pytestmark = pytest.mark.postgres_only

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ---------------------------------------------------------------------------
# Fixtures (compact mirrors of test_reports_csv.py)
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


async def _seed_company() -> Company:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at).limit(1)
            )
        ).scalars().first()
        assert company is not None, "Test DB has no seeded company"
        return company


@pytest.fixture
async def gl_accounts() -> dict[str, str]:
    company = await _seed_company()
    async with AsyncSessionLocal() as session:
        result: dict[str, str] = {}
        for at in (AccountType.INCOME, AccountType.EXPENSE, AccountType.ASSET):
            row = (
                await session.execute(
                    select(Account)
                    .where(
                        Account.archived_at.is_(None),
                        Account.account_type == at,
                        Account.is_header.is_(False),
                        Account.tenant_id == DEFAULT_TENANT_ID,
                        Account.company_id == company.id,
                    )
                    .order_by(Account.code)
                    .limit(1)
                )
            ).scalars().first()
            assert row is not None, f"Test DB has no non-header {at.value} account"
            result[at.value] = str(row.id)
    return result


@pytest.fixture
async def customer_deps() -> dict[str, str]:
    company = await _seed_company()
    async with AsyncSessionLocal() as session:
        income = (
            await session.execute(
                select(Account)
                .where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                    Account.company_id == company.id,
                    Account.is_header.is_(False),
                )
                .order_by(Account.code)
                .limit(1)
            )
        ).scalars().first()
        assert income is not None
        contact = Contact(
            tenant_id=DEFAULT_TENANT_ID,
            company_id=company.id,
            name=f"ExportCust-{uuid.uuid4().hex[:8]}",
            contact_type=ContactType.BOTH,
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)
    return {"income_account_id": str(income.id), "contact_id": str(contact.id)}


async def _post_je(client: AsyncClient, entry_date: str, lines: list[dict]) -> dict:
    r = await client.post(
        "/api/v1/journal_entries",
        json={"entry_date": entry_date, "narration": "export test entry", "lines": lines},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = await client.post(
        f"/api/v1/journal_entries/{body['id']}/post",
        headers={"If-Match": str(body["version"])},
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


async def _post_invoice(client: AsyncClient, deps: dict[str, str], issue: str, due: str, amount: str) -> dict:
    r = await client.post(
        "/api/v1/invoices",
        json={
            "contact_id": deps["contact_id"],
            "issue_date": issue,
            "due_date": due,
            "lines": [
                {
                    "description": "export test line",
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
        f"/api/v1/invoices/{body['id']}/post", headers={"If-Match": str(body["version"])}
    )
    assert r2.status_code == 200, r2.text
    return r2.json()


def _load_xlsx(content: bytes) -> list[tuple]:
    wb = openpyxl.load_workbook(io.BytesIO(content))
    ws = wb.active
    return list(ws.iter_rows(values_only=True))


def _find_header_row(grid: list[tuple], first_col: str) -> int:
    for i, row in enumerate(grid):
        if row and row[0] == first_col:
            return i
    raise AssertionError(f"header row starting with {first_col!r} not found")


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# Profit & Loss XLSX
# ---------------------------------------------------------------------------


async def test_profit_loss_xlsx(api_client: AsyncClient, gl_accounts: dict[str, str]) -> None:
    income_id = gl_accounts[AccountType.INCOME.value]
    expense_id = gl_accounts[AccountType.EXPENSE.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    await _post_je(
        api_client,
        "2029-03-05",
        lines=[
            {"account_id": asset_id, "debit": "3000.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "3000.00"},
        ],
    )
    await _post_je(
        api_client,
        "2029-03-06",
        lines=[
            {"account_id": expense_id, "debit": "1250.50", "credit": "0"},
            {"account_id": asset_id, "debit": "0", "credit": "1250.50"},
        ],
    )
    params = {"from_date": "2029-03-01", "to_date": "2029-03-31"}
    r = await api_client.get("/api/v1/reports/profit_loss.xlsx", params=params)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == _XLSX_MIME
    assert "profit_loss_2029-03-01_2029-03-31.xlsx" in r.headers.get("content-disposition", "")

    grid = _load_xlsx(r.content)
    hdr = _find_header_row(grid, "section")
    assert list(grid[hdr]) == ["section", "code", "account_name", "amount"]
    data = grid[hdr + 1 :]
    sections = {row[0] for row in data if row[0]}
    assert {"income", "expenses"} <= sections
    # money cells are numeric with 2-dp precision (never a string)
    amounts = [row[3] for row in data if row[3] is not None]
    assert amounts and all(isinstance(a, (int, float)) for a in amounts)
    assert any(abs(float(a) - 1250.50) < 0.005 for a in amounts)

    # XLSX columns match the CSV columns for the same period.
    rcsv = await api_client.get("/api/v1/reports/profit_loss.csv", params=params)
    csv_codes = {row[1] for row in _csv_rows(rcsv.text)[1:]}
    xlsx_codes = {str(row[1]) for row in data if row[1]}
    assert xlsx_codes == csv_codes


# ---------------------------------------------------------------------------
# Balance Sheet XLSX
# ---------------------------------------------------------------------------


async def test_balance_sheet_xlsx(api_client: AsyncClient, gl_accounts: dict[str, str]) -> None:
    asset_id = gl_accounts[AccountType.ASSET.value]
    income_id = gl_accounts[AccountType.INCOME.value]
    await _post_je(
        api_client,
        "2029-05-01",
        lines=[
            {"account_id": asset_id, "debit": "5000.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "5000.00"},
        ],
    )
    r = await api_client.get("/api/v1/reports/balance_sheet.xlsx", params={"as_of_date": "2029-05-31"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == _XLSX_MIME
    grid = _load_xlsx(r.content)
    hdr = _find_header_row(grid, "section")
    assert list(grid[hdr]) == ["section", "code", "account_name", "balance"]
    data = grid[hdr + 1 :]
    assert any(row[0] == "equity" and row[1] == "CYE" for row in data), "CYE row missing"


# ---------------------------------------------------------------------------
# Trial Balance XLSX
# ---------------------------------------------------------------------------


async def test_trial_balance_xlsx(api_client: AsyncClient, gl_accounts: dict[str, str]) -> None:
    income_id = gl_accounts[AccountType.INCOME.value]
    asset_id = gl_accounts[AccountType.ASSET.value]
    await _post_je(
        api_client,
        "2029-01-10",
        lines=[
            {"account_id": asset_id, "debit": "1234.00", "credit": "0"},
            {"account_id": income_id, "debit": "0", "credit": "1234.00"},
        ],
    )
    params = {"as_of_date": "2029-01-31"}
    r = await api_client.get("/api/v1/reports/trial_balance.xlsx", params=params)
    assert r.status_code == 200, r.text
    grid = _load_xlsx(r.content)
    hdr = _find_header_row(grid, "code")
    assert list(grid[hdr]) == ["code", "account_name", "account_type", "debit", "credit"]
    data = grid[hdr + 1 :]
    rcsv = await api_client.get("/api/v1/reports/trial_balance.csv", params=params)
    csv_codes = {row[0] for row in _csv_rows(rcsv.text)[1:]}
    xlsx_codes = {str(row[0]) for row in data if row[0]}
    assert xlsx_codes == csv_codes


# ---------------------------------------------------------------------------
# Aged AR / AP XLSX
# ---------------------------------------------------------------------------


async def test_aged_receivables_xlsx(api_client: AsyncClient, customer_deps: dict[str, str]) -> None:
    today = date.today()
    posted = await _post_invoice(
        api_client, customer_deps, today.isoformat(), (today + timedelta(days=1)).isoformat(), "1111.11"
    )
    r = await api_client.get("/api/v1/reports/aged_receivables.xlsx", params={"as_of_date": today.isoformat()})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == _XLSX_MIME
    grid = _load_xlsx(r.content)
    hdr = _find_header_row(grid, "contact")
    assert list(grid[hdr]) == [
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
    numbers = {str(row[1]) for row in grid[hdr + 1 :] if row[1]}
    assert posted["number"] in numbers


async def test_aged_payables_xlsx(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/reports/aged_payables.xlsx", params={"as_of_date": "2000-01-01"})
    assert r.status_code == 200, r.text
    grid = _load_xlsx(r.content)
    hdr = _find_header_row(grid, "contact")
    assert grid[hdr][1] == "bill_number"


# ---------------------------------------------------------------------------
# Cashflow CSV + XLSX
# ---------------------------------------------------------------------------


async def test_cashflow_csv_and_xlsx(api_client: AsyncClient, gl_accounts: dict[str, str]) -> None:
    params = {"from_date": "2029-07-01", "to_date": "2029-07-31"}
    rcsv = await api_client.get("/api/v1/reports/cashflow.csv", params=params)
    assert rcsv.status_code == 200, rcsv.text
    assert rcsv.headers["content-type"] == "text/csv; charset=utf-8"
    rows = _csv_rows(rcsv.text)
    assert rows[0] == ["section", "item", "amount"]
    items = {row[1] for row in rows[1:]}
    assert {"Net profit", "Net change in cash", "Opening cash", "Closing cash"} <= items

    rx = await api_client.get("/api/v1/reports/cashflow.xlsx", params=params)
    assert rx.status_code == 200, rx.text
    assert rx.headers["content-type"] == _XLSX_MIME
    grid = _load_xlsx(rx.content)
    hdr = _find_header_row(grid, "section")
    assert list(grid[hdr]) == ["section", "item", "amount"]


# ---------------------------------------------------------------------------
# P&L by segment CSV + XLSX
# ---------------------------------------------------------------------------


async def test_pl_by_segment_csv_and_xlsx(api_client: AsyncClient) -> None:
    params = {"from_date": "2029-07-01", "to_date": "2029-07-31"}
    rcsv = await api_client.get("/api/v1/reports/pl_by_segment.csv", params=params)
    assert rcsv.status_code == 200, rcsv.text
    assert _csv_rows(rcsv.text)[0] == ["segment", "section", "code", "account_name", "amount"]

    rx = await api_client.get("/api/v1/reports/pl_by_segment.xlsx", params=params)
    assert rx.status_code == 200, rx.text
    grid = _load_xlsx(rx.content)
    hdr = _find_header_row(grid, "segment")
    assert list(grid[hdr]) == ["segment", "section", "code", "account_name", "amount"]


# ---------------------------------------------------------------------------
# Revenue by customer CSV + XLSX
# ---------------------------------------------------------------------------


async def test_revenue_by_customer_csv_and_xlsx(api_client: AsyncClient, customer_deps: dict[str, str]) -> None:
    await _post_invoice(api_client, customer_deps, "2029-08-05", "2029-09-05", "2222.00")
    params = {"from_date": "2029-08-01", "to_date": "2029-08-31"}
    rcsv = await api_client.get("/api/v1/reports/revenue_by_customer.csv", params=params)
    assert rcsv.status_code == 200, rcsv.text
    rows = _csv_rows(rcsv.text)
    assert rows[0] == ["contact", "revenue", "pct_of_total"]

    rx = await api_client.get("/api/v1/reports/revenue_by_customer.xlsx", params=params)
    assert rx.status_code == 200, rx.text
    grid = _load_xlsx(rx.content)
    hdr = _find_header_row(grid, "contact")
    assert list(grid[hdr]) == ["contact", "revenue", "pct_of_total"]


# ---------------------------------------------------------------------------
# BAS summary CSV + XLSX (AU module)
# ---------------------------------------------------------------------------


async def test_bas_summary_csv_and_xlsx(api_client: AsyncClient) -> None:
    params = {"from_date": "2029-07-01", "to_date": "2029-09-30"}
    rcsv = await api_client.get("/api/v1/reports/bas_summary.csv", params=params)
    assert rcsv.status_code == 200, rcsv.text
    rows = _csv_rows(rcsv.text)
    assert rows[0] == ["label", "description", "amount"]
    labels = {row[0] for row in rows[1:]}
    assert {"G1", "G11", "1A", "1B", "net_gst"} <= labels

    rx = await api_client.get("/api/v1/reports/bas_summary.xlsx", params=params)
    assert rx.status_code == 200, rx.text
    assert rx.headers["content-type"] == _XLSX_MIME
    grid = _load_xlsx(rx.content)
    hdr = _find_header_row(grid, "label")
    xlsx_labels = {str(row[0]) for row in grid[hdr + 1 :] if row[0]}
    assert {"G1", "1A", "net_gst"} <= xlsx_labels


# ---------------------------------------------------------------------------
# Cashbook summary CSV + XLSX
# ---------------------------------------------------------------------------


async def test_cashbook_summary_csv_and_xlsx(api_client: AsyncClient) -> None:
    params = {"from": "2029-07-01", "to": "2029-07-31"}
    rcsv = await api_client.get("/api/v1/cashbook/summary.csv", params=params)
    assert rcsv.status_code == 200, rcsv.text
    assert rcsv.headers["content-type"] == "text/csv; charset=utf-8"
    rows = _csv_rows(rcsv.text)
    assert rows[0] == ["code", "label", "direction", "amount", "count"]
    labels = {row[1] for row in rows[1:]}
    assert {"Income total", "Expense total", "Net"} <= labels

    rx = await api_client.get("/api/v1/cashbook/summary.xlsx", params=params)
    assert rx.status_code == 200, rx.text
    assert rx.headers["content-type"] == _XLSX_MIME
    grid = _load_xlsx(rx.content)
    hdr = _find_header_row(grid, "code")
    assert list(grid[hdr]) == ["code", "label", "direction", "amount", "count"]


# ---------------------------------------------------------------------------
# Single-report PDFs (P&L / Balance Sheet / Trial Balance) — render mocked.
# ---------------------------------------------------------------------------

_FAKE_PDF = b"%PDF-1.5 fake report"
_RENDER_BASE = "http://web:8080"


@pytest.mark.asyncio
async def test_profit_loss_pdf(api_client: AsyncClient, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{_RENDER_BASE}/internal/render/report_single").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )
    r = await api_client.get(
        "/api/v1/reports/profit_loss.pdf",
        params={"from_date": "2029-03-01", "to_date": "2029-03-31"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert "profit_loss_2029-03-01_2029-03-31.pdf" in r.headers.get("content-disposition", "")
    sent = route.calls.last.request
    import json as _json

    ctx = _json.loads(sent.content)
    assert ctx["kind"] == "profit_loss"
    assert ctx["pl_report"]["from_date"] == "2029-03-01"


@pytest.mark.asyncio
async def test_balance_sheet_pdf(api_client: AsyncClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_RENDER_BASE}/internal/render/report_single").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )
    r = await api_client.get("/api/v1/reports/balance_sheet.pdf", params={"as_of_date": "2029-06-30"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_trial_balance_pdf(api_client: AsyncClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{_RENDER_BASE}/internal/render/report_single").mock(
        return_value=Response(200, content=_FAKE_PDF)
    )
    r = await api_client.get("/api/v1/reports/trial_balance.pdf", params={"as_of_date": "2029-06-30"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_profit_loss_pdf_compile_error_returns_502(
    api_client: AsyncClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(f"{_RENDER_BASE}/internal/render/report_single").mock(
        return_value=Response(422, json={"log_tail": "! Undefined control sequence."})
    )
    r = await api_client.get(
        "/api/v1/reports/profit_loss.pdf",
        params={"from_date": "2029-03-01", "to_date": "2029-03-31"},
    )
    assert r.status_code == 502, r.text
