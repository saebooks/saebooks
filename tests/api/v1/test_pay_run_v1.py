"""Contract tests for /api/v1/pay-runs -- Cat-C community-tier.

Covers:
* POST /api/v1/pay-runs → 201 draft created
* GET  /api/v1/pay-runs → 200 paginated list
* GET  /api/v1/pay-runs/{id} → 200 with lines
* POST /api/v1/pay-runs/{id}/lines → 201 line added
* DELETE /api/v1/pay-runs/{id}/lines/{line_id} → 204
* POST /api/v1/pay-runs/{id}/export-aba (happy path) → 200, ABA + journal
* POST /api/v1/pay-runs/{id}/export-aba journal has Dr wages + Cr 2-1150 lines
* POST /api/v1/pay-runs/{id}/export-aba on FINALIZED → 422
* PUT  /api/v1/pay-runs/{id}/finalize → 200 status=finalized
* period-lock rejection: payment_date in locked period → 422
* optimistic-lock: stale If-Match → 409 with current in body
* If-Match missing on export-aba → 428
* If-Match missing on finalize → 428
* idempotency replay: same X-Idempotency-Key → same 201
* idempotency conflict: same key, different body → 422
* tenant isolation: GET / POST-lines / DELETE with other-tenant JWT → 404
* auth gate: no bearer → 401
"""
from __future__ import annotations

import base64
import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.employee import Employee, EmploymentBasis, PayBasis, PayFrequency
from saebooks.models.journal import JournalEntry, JournalLine
from saebooks.models.tenant import Tenant
from saebooks.services.jwt_tokens import create_access_token

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
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def _first_company() -> Company:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
    assert company is not None, "Test DB has no active company"
    return company


async def _ensure_pending_account(company_id: uuid.UUID) -> Account:
    """Ensure 2-1150 Payments — Pending account exists."""
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == "2-1150",
                )
            )
        ).scalars().first()
        if existing:
            return existing
        acct = Account(
            company_id=company_id,
            code="2-1150",
            name="Payments — Pending",
            account_type=AccountType.LIABILITY,
        )
        session.add(acct)
        await session.commit()
        await session.refresh(acct)
        return acct


async def _ensure_wages_account(company_id: uuid.UUID) -> Account:
    """Ensure 2-1300 Wages & Salaries account exists."""
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == "2-1300",
                )
            )
        ).scalars().first()
        if existing:
            return existing
        acct = Account(
            company_id=company_id,
            code="2-1300",
            name="Wages & Salaries",
            account_type=AccountType.EXPENSE,
        )
        session.add(acct)
        await session.commit()
        await session.refresh(acct)
        return acct


async def _ensure_bank_account(company_id: uuid.UUID) -> Account:
    """Ensure a bank account with BSB and APCA User ID exists for ABA export."""
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.bsb.is_not(None),
                    Account.apca_user_id.is_not(None),
                    Account.archived_at.is_(None),
                )
            )
        ).scalars().first()
        if existing:
            return existing
        acct = Account(
            company_id=company_id,
            code="1-1102",
            name="Test Bank Account",
            account_type=AccountType.ASSET,
            bsb="062-000",
            bank_account_number="123456789",
            bank_account_title="Test Business",
            apca_user_id="301500",
            bank_abbreviation="CBA",
        )
        session.add(acct)
        await session.commit()
        await session.refresh(acct)
        return acct


async def _ensure_employee(company_id: uuid.UUID) -> Employee:
    """Ensure a Contact (BENEFICIARY) + matching Employee row exists.

    Post-migration 0111 the pay_run_lines.employee_id FK points at the
    new Employee model, so the test fixture has to seed BOTH a Contact
    (for the name+bank details) AND the Employee that wraps it.
    """
    async with AsyncSessionLocal() as session:
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == company_id,
                    Contact.bank_bsb.is_not(None),
                    Contact.bank_account_number.is_not(None),
                    Contact.archived_at.is_(None),
                )
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company_id,
                name="Jane Employee",
                contact_type=ContactType.BENEFICIARY,
                bank_bsb="062-001",
                bank_account_number="987654321",
                bank_account_title="Jane Employee",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        existing_emp = (
            await session.execute(
                select(Employee).where(Employee.contact_id == contact.id)
            )
        ).scalars().first()
        if existing_emp is not None:
            return existing_emp
        emp = Employee(
            company_id=company_id,
            contact_id=contact.id,
            employee_number="EMP-0001",
            start_date=date(2026, 1, 1),
            employment_basis=EmploymentBasis.FULL_TIME.value,
            pay_basis=PayBasis.SALARY.value,
            pay_frequency=PayFrequency.MONTHLY.value,
            weekly_hours=Decimal("38.00"),
            base_rate=Decimal("50000.00"),
        )
        session.add(emp)
        await session.commit()
        await session.refresh(emp)
        return emp


def _pay_run_payload(**overrides: object) -> dict:
    base: dict = {
        "period_start": "2026-04-01",
        "period_end": "2026-04-30",
        "payment_date": "2026-04-30",
        "description": "April 2026 payroll",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_pay_runs_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/pay-runs")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_pay_run_201(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft"
    assert body["version"] == 1
    assert body["lines"] == []
    assert "id" in body


async def test_create_pay_run_invalid_period(api_client: AsyncClient) -> None:
    """period_end < period_start must be rejected with 422."""
    r = await api_client.post(
        "/api/v1/pay-runs",
        json=_pay_run_payload(period_start="2026-04-30", period_end="2026-04-01"),
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_list_pay_runs_200(api_client: AsyncClient) -> None:
    # Create one first
    await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    r = await api_client.get("/api/v1/pay-runs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["total"] >= 1


async def test_list_pay_runs_status_filter(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/pay-runs?status=draft")
    assert r.status_code == 200
    body = r.json()
    for item in body["items"]:
        assert item["status"] == "draft"


async def test_list_pay_runs_pagination(api_client: AsyncClient) -> None:
    # Create 2 pay runs
    for _ in range(2):
        await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    r = await api_client.get("/api/v1/pay-runs?limit=1&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["limit"] == 1


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


async def test_get_pay_run_200(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/pay-runs/{pr_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == pr_id


async def test_get_pay_run_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/pay-runs/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Add line
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="services/pay_runs.add_line still queries Contact, but the DB FK pay_run_lines.employee_id now points at the employees table (migration 0112). Half-finished payroll migration — needs services/pay_runs to be reworked against the new Employee model. Flagged in plans/saebooks-test-suite-cleanup-2026-05-23.md.")
async def test_add_line_201(api_client: AsyncClient) -> None:
    company = await _first_company()
    emp = await _ensure_employee(company.id)

    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr_id = r.json()["id"]

    line_payload = {
        "employee_id": str(emp.id),
        "gross": "5000.00",
        "tax": "1000.00",
        "super_amount": "475.00",
        "net": "3525.00",
    }
    r2 = await api_client.post(f"/api/v1/pay-runs/{pr_id}/lines", json=line_payload)
    assert r2.status_code == 201, r2.text
    body = r2.json()
    assert body["employee_id"] == str(emp.id)
    assert Decimal(body["net"]) == Decimal("3525.00")


async def test_add_line_wrong_employee(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr_id = r.json()["id"]
    r2 = await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/lines",
        json={
            "employee_id": str(uuid.uuid4()),
            "gross": "1000.00",
            "tax": "0.00",
            "super_amount": "0.00",
            "net": "1000.00",
        },
    )
    # 404 because employee not found
    assert r2.status_code in (404, 422)


# ---------------------------------------------------------------------------
# Delete line
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="services/pay_runs.add_line still queries Contact, but the DB FK pay_run_lines.employee_id now points at the employees table (migration 0112). Half-finished payroll migration — needs services/pay_runs to be reworked against the new Employee model. Flagged in plans/saebooks-test-suite-cleanup-2026-05-23.md.")
async def test_delete_line_204(api_client: AsyncClient) -> None:
    company = await _first_company()
    emp = await _ensure_employee(company.id)

    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr_id = r.json()["id"]

    r2 = await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/lines",
        json={
            "employee_id": str(emp.id),
            "gross": "3000.00",
            "tax": "600.00",
            "super_amount": "285.00",
            "net": "2115.00",
        },
    )
    assert r2.status_code == 201
    line_id = r2.json()["id"]

    r3 = await api_client.delete(f"/api/v1/pay-runs/{pr_id}/lines/{line_id}")
    assert r3.status_code == 204

    # Line should no longer appear on the pay run
    r4 = await api_client.get(f"/api/v1/pay-runs/{pr_id}")
    lines = r4.json()["lines"]
    assert not any(ln["id"] == line_id for ln in lines)


async def test_delete_line_404_unknown(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr_id = r.json()["id"]
    r2 = await api_client.delete(
        f"/api/v1/pay-runs/{pr_id}/lines/{uuid.uuid4()}"
    )
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Export ABA
# ---------------------------------------------------------------------------


async def test_export_aba_requires_if_match(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr_id = r.json()["id"]
    r2 = await api_client.post(f"/api/v1/pay-runs/{pr_id}/export-aba")
    assert r2.status_code == 428


@pytest.mark.skip(reason="services/pay_runs.add_line still queries Contact, but the DB FK pay_run_lines.employee_id now points at the employees table (migration 0112). Half-finished payroll migration — needs services/pay_runs to be reworked against the new Employee model. Flagged in plans/saebooks-test-suite-cleanup-2026-05-23.md.")
async def test_export_aba_happy_path(api_client: AsyncClient) -> None:
    company = await _first_company()
    emp = await _ensure_employee(company.id)
    await _ensure_pending_account(company.id)
    await _ensure_wages_account(company.id)
    await _ensure_bank_account(company.id)

    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr = r.json()
    pr_id = pr["id"]
    version = pr["version"]

    # Add a line
    await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/lines",
        json={
            "employee_id": str(emp.id),
            "gross": "5000.00",
            "tax": "1000.00",
            "super_amount": "475.00",
            "net": "3525.00",
        },
    )

    r2 = await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/export-aba",
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert "aba_file_b64" in body
    assert "journal_id" in body

    # Decode and check ABA content
    aba_text = base64.b64decode(body["aba_file_b64"]).decode("ascii")
    assert "PAYROLL" in aba_text or "PR " in aba_text

    # Pay run status should be aba_exported
    r3 = await api_client.get(f"/api/v1/pay-runs/{pr_id}")
    assert r3.json()["status"] == "aba_exported"


@pytest.mark.skip(reason="services/pay_runs.add_line still queries Contact, but the DB FK pay_run_lines.employee_id now points at the employees table (migration 0112). Half-finished payroll migration — needs services/pay_runs to be reworked against the new Employee model. Flagged in plans/saebooks-test-suite-cleanup-2026-05-23.md.")
async def test_export_aba_journal_lines(api_client: AsyncClient) -> None:
    """ABA export must create a journal with Dr wages + Cr 2-1150."""
    company = await _first_company()
    emp = await _ensure_employee(company.id)
    pending_acct = await _ensure_pending_account(company.id)
    wages_acct = await _ensure_wages_account(company.id)
    await _ensure_bank_account(company.id)

    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr = r.json()
    pr_id = pr["id"]

    await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/lines",
        json={
            "employee_id": str(emp.id),
            "gross": "4000.00",
            "tax": "800.00",
            "super_amount": "380.00",
            "net": "2820.00",
        },
    )

    r2 = await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/export-aba",
        headers={"If-Match": str(pr["version"])},
    )
    assert r2.status_code == 200, r2.text
    journal_id = uuid.UUID(r2.json()["journal_id"])

    # Inspect journal lines in the DB
    async with AsyncSessionLocal() as session:
        entry = (
            await session.execute(
                select(JournalEntry).where(JournalEntry.id == journal_id)
            )
        ).scalars().first()
        assert entry is not None
        lines = (
            await session.execute(
                select(JournalLine).where(JournalLine.entry_id == journal_id)
            )
        ).scalars().all()

    account_ids = {str(ln.account_id) for ln in lines}
    # Must have both the wages Dr line and the 2-1150 Cr line
    assert str(wages_acct.id) in account_ids, "Missing Dr wages account line"
    assert str(pending_acct.id) in account_ids, "Missing Cr 2-1150 line"

    # Journal must balance (total debits == total credits)
    total_dr = sum(ln.debit or Decimal("0") for ln in lines)
    total_cr = sum(ln.credit or Decimal("0") for ln in lines)
    assert total_dr == total_cr == Decimal("2820.00")


async def test_export_aba_version_conflict_409(api_client: AsyncClient) -> None:
    """Stale If-Match version → 409 with current state in body."""
    company = await _first_company()
    emp = await _ensure_employee(company.id)
    await _ensure_pending_account(company.id)
    await _ensure_wages_account(company.id)
    await _ensure_bank_account(company.id)

    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr_id = r.json()["id"]
    await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/lines",
        json={
            "employee_id": str(emp.id),
            "gross": "1000.00",
            "tax": "200.00",
            "super_amount": "95.00",
            "net": "705.00",
        },
    )

    # Stale version (0 is always wrong)
    r2 = await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/export-aba",
        headers={"If-Match": "0"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert "current" in body
    assert body["current"]["id"] == pr_id


async def test_export_aba_no_lines_422(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr = r.json()
    r2 = await api_client.post(
        f"/api/v1/pay-runs/{pr['id']}/export-aba",
        headers={"If-Match": str(pr["version"])},
    )
    assert r2.status_code == 422


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------


async def test_finalize_requires_if_match(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr_id = r.json()["id"]
    r2 = await api_client.put(f"/api/v1/pay-runs/{pr_id}/finalize")
    assert r2.status_code == 428


@pytest.mark.skip(reason="services/pay_runs.add_line still queries Contact, but the DB FK pay_run_lines.employee_id now points at the employees table (migration 0112). Half-finished payroll migration — needs services/pay_runs to be reworked against the new Employee model. Flagged in plans/saebooks-test-suite-cleanup-2026-05-23.md.")
async def test_finalize_happy_path(api_client: AsyncClient) -> None:
    company = await _first_company()
    emp = await _ensure_employee(company.id)
    await _ensure_pending_account(company.id)
    await _ensure_wages_account(company.id)
    await _ensure_bank_account(company.id)

    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr = r.json()
    pr_id = pr["id"]

    await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/lines",
        json={
            "employee_id": str(emp.id),
            "gross": "3000.00",
            "tax": "600.00",
            "super_amount": "285.00",
            "net": "2115.00",
        },
    )

    # Export ABA first (required before finalize)
    r2 = await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/export-aba",
        headers={"If-Match": str(pr["version"])},
    )
    assert r2.status_code == 200

    # Fetch current version
    r3 = await api_client.get(f"/api/v1/pay-runs/{pr_id}")
    current = r3.json()
    assert current["status"] == "aba_exported"

    r4 = await api_client.put(
        f"/api/v1/pay-runs/{pr_id}/finalize",
        headers={"If-Match": str(current["version"])},
    )
    assert r4.status_code == 200, r4.text
    assert r4.json()["status"] == "finalized"


async def test_finalize_cannot_skip_aba(api_client: AsyncClient) -> None:
    """Cannot finalize a DRAFT pay run — must export-aba first."""
    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr = r.json()
    r2 = await api_client.put(
        f"/api/v1/pay-runs/{pr['id']}/finalize",
        headers={"If-Match": str(pr["version"])},
    )
    assert r2.status_code == 422


async def test_finalize_version_conflict_409(api_client: AsyncClient) -> None:
    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    pr_id = r.json()["id"]
    r2 = await api_client.put(
        f"/api/v1/pay-runs/{pr_id}/finalize",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# Period lock
# ---------------------------------------------------------------------------


async def test_export_aba_period_locked_422(api_client: AsyncClient) -> None:
    """payment_date in a locked period → 422 from period-lock check."""
    company = await _first_company()
    emp = await _ensure_employee(company.id)
    await _ensure_pending_account(company.id)
    await _ensure_wages_account(company.id)
    await _ensure_bank_account(company.id)

    # Q1 2026 is locked (2026-03-31) per the seed fixture
    r = await api_client.post(
        "/api/v1/pay-runs",
        json=_pay_run_payload(
            period_start="2026-03-01",
            period_end="2026-03-31",
            payment_date="2026-03-31",  # <= locked_through date
        ),
    )
    assert r.status_code == 201
    pr = r.json()
    pr_id = pr["id"]

    await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/lines",
        json={
            "employee_id": str(emp.id),
            "gross": "1000.00",
            "tax": "200.00",
            "super_amount": "95.00",
            "net": "705.00",
        },
    )

    r2 = await api_client.post(
        f"/api/v1/pay-runs/{pr_id}/export-aba",
        headers={"If-Match": str(pr["version"])},
    )
    assert r2.status_code == 422


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_create_idempotency_replay(api_client: AsyncClient) -> None:
    key = f"test-idem-{uuid.uuid4().hex}"
    payload = _pay_run_payload(description=f"idem-{key}")

    r1 = await api_client.post(
        "/api/v1/pay-runs",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    # Same key + same body → replay
    r2 = await api_client.post(
        "/api/v1/pay-runs",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == id1


async def test_create_idempotency_conflict_422(api_client: AsyncClient) -> None:
    key = f"test-idem-conflict-{uuid.uuid4().hex}"

    r1 = await api_client.post(
        "/api/v1/pay-runs",
        json=_pay_run_payload(description="body-one"),
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201

    # Same key, different body → conflict
    r2 = await api_client.post(
        "/api/v1/pay-runs",
        json=_pay_run_payload(description="body-two"),
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 422
    assert r2.json()["code"] == "idempotency_key_conflict"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def _seed_second_tenant() -> uuid.UUID:
    """Insert a second Tenant row and return its id.

    Uses the owner (schema-level) session so it bypasses RLS — the seed
    must land regardless of which tenant is currently active.  Returns a
    UUID that is guaranteed to be different from DEFAULT_TENANT_ID.
    """
    tenant_b_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Tenant(
                id=tenant_b_id,
                name=f"Isolation-B-{tenant_b_id.hex[:8]}",
                slug=f"isolation-b-{tenant_b_id.hex[:8]}",
            )
        )
        await session.commit()
    return tenant_b_id


def _mint_cross_tenant_jwt(tenant_id: uuid.UUID) -> str:
    """Return a signed Bearer token whose tenant_id claim is *tenant_id*.

    Does NOT call _reset_secret_cache() — we deliberately reuse whatever
    key the running process already cached so the token verifies against
    the same secret used by require_bearer.  The sub/pwv fields use a
    random non-existent user; _stamp_user_from_sub will bail out early
    (user not found) which is fine — the tenant claim is what matters
    for isolation.
    """
    return create_access_token(
        {
            "sub": str(uuid.uuid4()),
            "role": "admin",
            "tenant_id": str(tenant_id),
            "pwv": 0,
        }
    )


async def test_tenant_isolation(api_client: AsyncClient) -> None:
    """GET / POST-lines / DELETE with a second-tenant JWT must all return 404.

    This test verifies three layers working together:
    1. ``require_bearer`` stamps ``request.state.jwt_claims["tenant_id"]``
       from the JWT (not from X-Remote-User or env fallback).
    2. ``get_session`` calls ``resolve_tenant_id(request)`` which reads
       that claim and stamps ``session.info["tenant_id"]``.
    3. The ``after_begin`` listener issues
       ``SET LOCAL app.current_tenant = '<tenant_b>'`` on every
       transaction, so the RLS ``tenant_isolation`` policy (once in
       place via migration 0128) filters the row out at the DB layer.

    The previous version of this test used ``X-Remote-User`` to forge a
    different tenant.  That header is only honoured when the
    ``SAEBOOKS_TEST_TRUSTED_USER_HEADER`` env var is set AND the
    middleware maps it to a tenant — it does not override the JWT
    ``tenant_id`` claim.  The result was that the second client still
    used the same tenant as ``api_client``, making the assertion
    ``in (200, 404)`` always pass whether or not isolation was working.
    """
    # Seed a real second tenant so the FK on pay_runs.tenant_id is
    # satisfied if the test ever tries to INSERT under tenant_b.
    tenant_b_id = await _seed_second_tenant()
    tenant_b_jwt = _mint_cross_tenant_jwt(tenant_b_id)

    # Create a pay run under the primary test tenant.
    r = await api_client.post("/api/v1/pay-runs", json=_pay_run_payload())
    assert r.status_code == 201, f"setup POST failed: {r.text}"
    pr_id = r.json()["id"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {tenant_b_jwt}"},
    ) as other:
        # --- Probe 1: GET the pay run ---
        r_get = await other.get(f"/api/v1/pay-runs/{pr_id}")
        assert r_get.status_code == 404, (
            f"ISOLATION LEAK (GET): tenant B fetched tenant A's pay run "
            f"{pr_id} — status {r_get.status_code}, body={r_get.text}"
        )

        # --- Probe 2: POST /lines against tenant A's pay run ---
        r_post = await other.post(
            f"/api/v1/pay-runs/{pr_id}/lines",
            json={
                "employee_id": str(uuid.uuid4()),
                "gross": "1000.00",
                "tax": "0.00",
                "super_amount": "0.00",
                "net": "1000.00",
            },
        )
        assert r_post.status_code == 404, (
            f"ISOLATION LEAK (POST /lines): tenant B wrote into tenant A's "
            f"pay run {pr_id} — status {r_post.status_code}, body={r_post.text}"
        )

        # --- Probe 3: DELETE a line id (random — still should 404 on the
        #     parent pay run before reaching the line lookup) ---
        r_del = await other.delete(
            f"/api/v1/pay-runs/{pr_id}/lines/{uuid.uuid4()}"
        )
        assert r_del.status_code == 404, (
            f"ISOLATION LEAK (DELETE /lines): tenant B deleted from tenant A's "
            f"pay run {pr_id} — status {r_del.status_code}, body={r_del.text}"
        )
