"""Employee service — CRUD + TFN handling + payroll-related lookups.

Employees are 1:1 with Contact: the Contact carries demographic data
(name, email, phone, address) and the Employee carries the payroll-
specific overlay. The contact_id FK is UNIQUE — one contact maps to
at most one employee.

TFN is stored Fernet-encrypted (saebooks.services.crypto). The API
layer masks the TFN in responses unless the caller has
``employee.tfn_view``. Decryption is audit-logged at the route layer.

Workflow notes:
- ``create()`` mints an employee_number via ``services.numbering`` if
  the caller doesn't pass one.
- ``terminate()`` sets end_date + termination_reason in one txn (the
  CHECK constraint ``ck_employees_termination_needs_end_date``
  guarantees the pair stays consistent).
- ``archive()`` is a soft-delete; refuses if the employee has any
  non-VOIDED pay_run_lines (placeholder until Phase 1B's pay-run
  retarget — for now we just archive without that check).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.employee import (
    Employee,
    EmploymentBasis,
    IncomeStreamType,
    PayBasis,
    PayFrequency,
    PayslipDelivery,
    TerminationReason,
    TfnStatus,
)
from saebooks.services import crypto, numbering
from saebooks.services import super_funds as super_funds_svc

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class EmployeeError(Exception):
    def __init__(self, message: str, *, code: str = "employee_error") -> None:
        super().__init__(message)
        self.code = code


def _encrypt_opt(value: str | None) -> str | None:
    return crypto.encrypt_field(value) if value else None


def _decrypt_opt(value: str | None) -> str | None:
    return crypto.decrypt_field(value) if value else None


def _mask_tfn(plain: str | None) -> str | None:
    """Mask a TFN as 'XXX-XXX-NNN' (show last 3 digits only)."""
    if not plain:
        return None
    digits = "".join(c for c in plain if c.isdigit())
    if len(digits) < 3:
        return "XXX-XXX-XXX"
    return f"XXX-XXX-{digits[-3:]}"


@dataclass
class EmployeeBankDecrypted:
    bsb: str | None
    account_number: str | None
    account_name: str | None


# --- create / read / update / terminate / archive --------------------------


async def create(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
    start_date: date,
    employment_basis: EmploymentBasis | str,
    base_rate: Decimal,
    employee_number: str | None = None,
    tfn: str | None = None,
    tfn_status: TfnStatus | str = TfnStatus.NOT_PROVIDED,
    dob: date | None = None,
    address_line1: str | None = None,
    address_line2: str | None = None,
    suburb: str | None = None,
    state: str | None = None,
    postcode: str | None = None,
    country_code: str = "AU",
    claims_tax_free_threshold: bool = False,
    is_australian_resident: bool = True,
    study_training_support_loan: bool = False,
    working_holiday_maker: bool = False,
    whm_country_code: str | None = None,
    income_stream_type: IncomeStreamType | str = IncomeStreamType.SALARY_WAGES,
    payg_branch_code: str | None = None,
    bsb: str | None = None,
    account_number: str | None = None,
    account_name: str | None = None,
    super_fund_id: uuid.UUID | None = None,
    super_member_number: str | None = None,
    payslip_email: str | None = None,
    payslip_delivery: PayslipDelivery | str = PayslipDelivery.EMAIL,
    pay_frequency: PayFrequency | str = PayFrequency.WEEKLY,
    pay_basis: PayBasis | str = PayBasis.HOURLY,
    weekly_hours: Decimal = Decimal("38.00"),
    tax_treatment_code: str | None = None,
    notes: str | None = None,
    extra: dict[str, Any] | None = None,
    tenant_id: uuid.UUID = _DEFAULT_TENANT_ID,
) -> Employee:
    # Cross-field validation that mirrors the DB CHECK constraints —
    # surface a friendlier error than the DB's constraint name.
    if base_rate < 0:
        raise EmployeeError("base_rate cannot be negative", code="invalid_base_rate")
    if working_holiday_maker and not whm_country_code:
        raise EmployeeError(
            "whm_country_code required when working_holiday_maker is True",
            code="missing_whm_country",
        )
    pay_basis_v = pay_basis.value if hasattr(pay_basis, "value") else str(pay_basis)
    if pay_basis_v == PayBasis.SALARY.value and weekly_hours <= 0:
        raise EmployeeError(
            "weekly_hours must be > 0 for salaried employees",
            code="invalid_weekly_hours",
        )

    # Default super fund if not provided.
    if super_fund_id is None:
        default_fund = await super_funds_svc.get_default(
            session, company_id=company_id
        )
        if default_fund is not None:
            super_fund_id = default_fund.id

    # Mint employee_number if not supplied.
    if not employee_number:
        employee_number = await numbering.next_number(
            session, company_id=company_id, kind="employee"
        )

    employee = Employee(
        company_id=company_id,
        tenant_id=tenant_id,
        contact_id=contact_id,
        employee_number=employee_number,
        tfn_encrypted=_encrypt_opt(tfn),
        tfn_status=(tfn_status.value if hasattr(tfn_status, "value") else str(tfn_status)),
        dob=dob,
        start_date=start_date,
        address_line1=address_line1,
        address_line2=address_line2,
        suburb=suburb,
        state=state,
        postcode=postcode,
        country_code=country_code,
        employment_basis=(employment_basis.value if hasattr(employment_basis, "value") else str(employment_basis)),
        tax_treatment_code=tax_treatment_code,
        claims_tax_free_threshold=claims_tax_free_threshold,
        is_australian_resident=is_australian_resident,
        study_training_support_loan=study_training_support_loan,
        working_holiday_maker=working_holiday_maker,
        whm_country_code=whm_country_code,
        income_stream_type=(income_stream_type.value if hasattr(income_stream_type, "value") else str(income_stream_type)),
        payg_branch_code=payg_branch_code,
        bsb_encrypted=_encrypt_opt(bsb),
        account_number_encrypted=_encrypt_opt(account_number),
        account_name_encrypted=_encrypt_opt(account_name),
        super_fund_id=super_fund_id,
        super_member_number=super_member_number,
        payslip_email=payslip_email,
        payslip_delivery=(payslip_delivery.value if hasattr(payslip_delivery, "value") else str(payslip_delivery)),
        pay_frequency=(pay_frequency.value if hasattr(pay_frequency, "value") else str(pay_frequency)),
        pay_basis=pay_basis_v,
        base_rate=base_rate,
        weekly_hours=weekly_hours,
        notes=notes,
        extra=extra,
    )
    session.add(employee)
    await session.flush()
    await session.refresh(employee)
    return employee


async def get(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> Employee | None:
    stmt = sa.select(Employee).where(
        Employee.company_id == company_id,
        Employee.id == employee_id,
        Employee.archived_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_by_contact(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    contact_id: uuid.UUID,
) -> Employee | None:
    stmt = sa.select(Employee).where(
        Employee.company_id == company_id,
        Employee.contact_id == contact_id,
        Employee.archived_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@dataclass
class EmployeeFilters:
    employment_basis: str | None = None
    only_active: bool = True  # i.e. end_date IS NULL
    super_fund_id: uuid.UUID | None = None
    search: str | None = None  # matches employee_number


async def list_employees(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    filters: EmployeeFilters | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Employee], int]:
    filters = filters or EmployeeFilters()
    where = [Employee.company_id == company_id, Employee.archived_at.is_(None)]
    if filters.employment_basis:
        where.append(Employee.employment_basis == filters.employment_basis)
    if filters.only_active:
        where.append(Employee.end_date.is_(None))
    if filters.super_fund_id:
        where.append(Employee.super_fund_id == filters.super_fund_id)
    if filters.search:
        like = f"%{filters.search}%"
        where.append(Employee.employee_number.ilike(like))

    count_stmt = sa.select(sa.func.count()).select_from(Employee).where(*where)
    total = (await session.execute(count_stmt)).scalar_one()
    items_stmt = (
        sa.select(Employee)
        .where(*where)
        .order_by(Employee.employee_number)
        .limit(limit).offset(offset)
    )
    items = list((await session.execute(items_stmt)).scalars().all())
    return items, int(total)


async def update(
    session: AsyncSession,
    *,
    employee: Employee,
    expected_version: int | None = None,
    **fields: Any,
) -> Employee:
    if expected_version is not None and employee.version != expected_version:
        raise EmployeeError(
            f"version mismatch: expected {expected_version}, got {employee.version}",
            code="version_mismatch",
        )

    SIMPLE = {
        "employee_number", "previous_payee_id",
        "dob", "start_date", "address_line1", "address_line2",
        "suburb", "state", "postcode", "country_code",
        "employment_basis", "tax_treatment_code",
        "claims_tax_free_threshold", "is_australian_resident",
        "study_training_support_loan", "working_holiday_maker",
        "whm_country_code", "income_stream_type", "payg_branch_code",
        "super_fund_id", "super_member_number",
        "payslip_email", "payslip_delivery",
        "pay_frequency", "pay_basis", "base_rate", "weekly_hours",
        "tfn_status", "notes", "extra",
    }
    ENCRYPTED_MAP = {
        "tfn": "tfn_encrypted",
        "bsb": "bsb_encrypted",
        "account_number": "account_number_encrypted",
        "account_name": "account_name_encrypted",
    }

    for name, value in fields.items():
        if name in SIMPLE:
            setattr(employee, name, value)
        elif name in ENCRYPTED_MAP:
            setattr(employee, ENCRYPTED_MAP[name], _encrypt_opt(value))

    employee.version += 1
    await session.flush()
    await session.refresh(employee)
    return employee


async def terminate(
    session: AsyncSession,
    *,
    employee: Employee,
    end_date: date,
    reason: TerminationReason | str,
) -> Employee:
    if employee.end_date is not None:
        raise EmployeeError(
            "employee is already terminated", code="already_terminated"
        )
    if end_date < employee.start_date:
        raise EmployeeError(
            "end_date cannot be before start_date", code="invalid_end_date"
        )
    employee.end_date = end_date
    employee.termination_reason = (
        reason.value if hasattr(reason, "value") else str(reason)
    )
    employee.version += 1
    await session.flush()
    await session.refresh(employee)
    return employee


async def archive(
    session: AsyncSession,
    *,
    employee: Employee,
) -> Employee:
    employee.archived_at = datetime.now(UTC)
    await session.flush()
    return employee


# --- sensitive accessors (caller must audit-log) ---------------------------


def decrypt_tfn(employee: Employee) -> str | None:
    """Return plaintext TFN. Caller MUST audit-log this access."""
    return _decrypt_opt(employee.tfn_encrypted)


def masked_tfn(employee: Employee) -> str | None:
    """Return 'XXX-XXX-NNN' style mask of the TFN.

    Safe for non-privileged display. Uses the last 3 digits of the
    decrypted TFN; if the column is empty, returns None.
    """
    plain = decrypt_tfn(employee)
    return _mask_tfn(plain)


def decrypt_bank(employee: Employee) -> EmployeeBankDecrypted:
    """Return plaintext bank fields. Caller MUST audit-log this access."""
    return EmployeeBankDecrypted(
        bsb=_decrypt_opt(employee.bsb_encrypted),
        account_number=_decrypt_opt(employee.account_number_encrypted),
        account_name=_decrypt_opt(employee.account_name_encrypted),
    )
