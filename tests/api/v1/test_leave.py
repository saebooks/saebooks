"""Contract tests for /api/v1/leave.

Gap noted in the 2026-05-23 overnight regression sweep — leave router
(employee leave balances + adjust + opening-balance) shipped with the
payroll Phase 4 work (migration 0115) with zero contract coverage.

Covers:
* Auth gate (401 without bearer).
* GET balances — 200, empty list before any accrual / adjust.
* POST adjust — creates a balance row if absent, sets balance_hours.
* POST adjust — second adjust accumulates delta.
* POST adjust — empty reason → 400 (missing_reason).
* POST adjust — missing reason field → 422 (Pydantic min_length=1).
* POST opening — sets opening_balance_hours and opening_balance_as_at.
* POST opening — invalid as_at format → 400.
* POST opening — does NOT overwrite running balance.
"""
from __future__ import annotations

import uuid
from datetime import date as _date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.employee import Employee
from saebooks.models.leave import LeaveAccrual, LeaveBalance


_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


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


async def _seed_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
    assert company is not None
    return company.id


async def _seed_employee() -> uuid.UUID:
    """Create a fresh Employee + Contact for the test, return employee id."""
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        contact = Contact(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            name=f"Pytest Leave EE {uuid.uuid4()}",
            contact_type=ContactType.BOTH,
        )
        session.add(contact)
        await session.flush()
        emp = Employee(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            contact_id=contact.id,
            employee_number=f"LE{uuid.uuid4().hex[:6]}",
            start_date=_date(2026, 1, 1),
            employment_basis="F",
            base_rate=Decimal("35.00"),
            tfn_status="NOT_PROVIDED",
        )
        session.add(emp)
        await session.commit()
        await session.refresh(emp)
        return emp.id


async def _purge_leave_state() -> None:
    """Clear LeaveBalance + LeaveAccrual rows across the seed company so
    the GET balances test starts empty regardless of previous tests."""
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        for accrual in (
            await session.execute(
                select(LeaveAccrual).where(LeaveAccrual.company_id == company_id)
            )
        ).scalars().all():
            await session.delete(accrual)
        for balance in (
            await session.execute(
                select(LeaveBalance).where(LeaveBalance.company_id == company_id)
            )
        ).scalars().all():
            await session.delete(balance)
        await session.commit()


@pytest.fixture(autouse=True)
async def _clean_leave() -> None:
    await _purge_leave_state()
    yield
    await _purge_leave_state()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_balances_requires_bearer(unauth_client: AsyncClient) -> None:
    eid = uuid.uuid4()
    resp = await unauth_client.get(f"/api/v1/leave/balances/{eid}")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_adjust_requires_bearer(unauth_client: AsyncClient) -> None:
    eid = uuid.uuid4()
    resp = await unauth_client.post(
        f"/api/v1/leave/balances/{eid}/adjust",
        json={"leave_type": "ANNUAL", "delta_hours": "1.0", "reason": "x"},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# GET balances
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_balances_empty_for_new_employee(api_client: AsyncClient) -> None:
    eid = await _seed_employee()
    resp = await api_client.get(f"/api/v1/leave/balances/{eid}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# ---------------------------------------------------------------------------
# POST adjust
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adjust_creates_balance(api_client: AsyncClient) -> None:
    eid = await _seed_employee()
    resp = await api_client.post(
        f"/api/v1/leave/balances/{eid}/adjust",
        json={
            "leave_type": "ANNUAL",
            "delta_hours": "5.5",
            "reason": "Test opening accrual",
        },
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["employee_id"] == str(eid)
    assert out["leave_type"] == "ANNUAL"
    assert Decimal(out["balance_hours"]) == Decimal("5.50")
    assert out["version"] >= 1


@pytest.mark.asyncio
async def test_adjust_accumulates(api_client: AsyncClient) -> None:
    eid = await _seed_employee()
    body = {"leave_type": "ANNUAL", "delta_hours": "4.0", "reason": "first"}
    first = await api_client.post(
        f"/api/v1/leave/balances/{eid}/adjust", json=body
    )
    assert first.status_code == 200, first.text
    second = await api_client.post(
        f"/api/v1/leave/balances/{eid}/adjust",
        json={"leave_type": "ANNUAL", "delta_hours": "-1.5", "reason": "second"},
    )
    assert second.status_code == 200, second.text
    out = second.json()
    assert Decimal(out["balance_hours"]) == Decimal("2.50")


@pytest.mark.asyncio
async def test_adjust_empty_reason_400(api_client: AsyncClient) -> None:
    eid = await _seed_employee()
    resp = await api_client.post(
        f"/api/v1/leave/balances/{eid}/adjust",
        json={"leave_type": "ANNUAL", "delta_hours": "1.0", "reason": "   "},
    )
    # Pydantic min_length=1 would 422 the empty literal; whitespace makes it
    # past the schema and hits the service-layer missing_reason → 400.
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_adjust_missing_reason_field_422(api_client: AsyncClient) -> None:
    eid = await _seed_employee()
    resp = await api_client.post(
        f"/api/v1/leave/balances/{eid}/adjust",
        json={"leave_type": "ANNUAL", "delta_hours": "1.0"},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# POST opening balance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opening_balance_sets_value(api_client: AsyncClient) -> None:
    eid = await _seed_employee()
    resp = await api_client.post(
        f"/api/v1/leave/balances/{eid}/opening",
        json={
            "leave_type": "ANNUAL",
            "hours": "80.0",
            "as_at": "2026-07-01",
        },
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert Decimal(out["opening_balance_hours"]) == Decimal("80.00")
    assert out["opening_balance_as_at"] is not None
    assert out["opening_balance_as_at"].startswith("2026-07-01")


@pytest.mark.asyncio
async def test_opening_balance_invalid_as_at_400(api_client: AsyncClient) -> None:
    eid = await _seed_employee()
    resp = await api_client.post(
        f"/api/v1/leave/balances/{eid}/opening",
        json={
            "leave_type": "ANNUAL",
            "hours": "10.0",
            "as_at": "definitely-not-an-iso-date",
        },
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_opening_balance_does_not_overwrite_running(
    api_client: AsyncClient,
) -> None:
    """Per services.leave docstring — set_opening_balance leaves the
    running balance_hours unchanged. Verify with a sequence:
    adjust → opening → balances list shows opening_balance_hours set
    and balance_hours unchanged."""
    eid = await _seed_employee()
    first = await api_client.post(
        f"/api/v1/leave/balances/{eid}/adjust",
        json={"leave_type": "ANNUAL", "delta_hours": "7.0", "reason": "preexisting"},
    )
    assert first.status_code == 200, first.text
    opening = await api_client.post(
        f"/api/v1/leave/balances/{eid}/opening",
        json={"leave_type": "ANNUAL", "hours": "40.0"},
    )
    assert opening.status_code == 200, opening.text
    list_resp = await api_client.get(f"/api/v1/leave/balances/{eid}")
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert len(items) == 1
    item = items[0]
    assert Decimal(item["opening_balance_hours"]) == Decimal("40.00")
    # Running balance untouched by opening-balance call.
    assert Decimal(item["balance_hours"]) == Decimal("7.00")
