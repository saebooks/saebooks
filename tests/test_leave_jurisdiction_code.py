"""Test for ``LeaveBalance.jurisdiction_code`` (M1.5 P1 tail) — statutory-
scheme jurisdiction tag, NULL = AU (the implicit default for every
existing balance).
"""
from __future__ import annotations

import uuid
from datetime import date as _date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.employee import Employee
from saebooks.models.leave import LeaveBalance, LeaveType

pytestmark = pytest.mark.postgres_only

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _seed_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
    assert company is not None
    return company.id


async def _fresh_employee_id() -> uuid.UUID:
    company_id = await _seed_company_id()
    async with AsyncSessionLocal() as session:
        contact = Contact(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            name=f"Pytest LeaveJur EE {uuid.uuid4()}",
            contact_type=ContactType.BOTH,
        )
        session.add(contact)
        await session.flush()
        emp = Employee(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            contact_id=contact.id,
            employee_number=f"LJ{uuid.uuid4().hex[:6]}",
            start_date=_date(2026, 1, 1),
            employment_basis="F",
            base_rate=Decimal("35.00"),
            tfn_status="NOT_PROVIDED",
        )
        session.add(emp)
        await session.commit()
        await session.refresh(emp)
        return emp.id


async def test_leave_balance_jurisdiction_code_defaults_null() -> None:
    company_id = await _seed_company_id()
    employee_id = await _fresh_employee_id()
    async with AsyncSessionLocal() as session:
        balance = LeaveBalance(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            employee_id=employee_id,
            leave_type=LeaveType.ANNUAL,
        )
        session.add(balance)
        await session.commit()
        await session.refresh(balance)
        assert balance.jurisdiction_code is None


async def test_leave_balance_jurisdiction_code_settable() -> None:
    company_id = await _seed_company_id()
    employee_id = await _fresh_employee_id()
    async with AsyncSessionLocal() as session:
        balance = LeaveBalance(
            tenant_id=_DEFAULT_TENANT_ID,
            company_id=company_id,
            employee_id=employee_id,
            leave_type=LeaveType.ANNUAL,
            jurisdiction_code="NZ",
        )
        session.add(balance)
        await session.commit()
        await session.refresh(balance)
        assert balance.jurisdiction_code == "NZ"
