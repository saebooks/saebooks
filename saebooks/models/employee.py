"""Employee model for AU payroll + STP Phase 2 reporting.

One-to-one with Contact: the Contact carries the demographic / contact
fields (name, address, email, phone); the Employee carries the
payroll-specific overlay (DOB, TFN, employment terms, super fund, bank
account for net pay).

Sensitive fields (TFN, bank BSB + account number + account name) are
stored as Fernet ciphertext via ``saebooks.services.crypto``. The
plaintext NEVER touches the column — the service layer is the only
caller that holds plaintext, briefly, between request and insert.

RLS: ``tenant_isolation`` policy installed in migration
``0110_employees_and_super_funds``. CompanyScoped mixin applies the
per-request company filter via the global listener.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --- STP2 enums --------------------------------------------------------- #


class TfnStatus(enum.StrEnum):
    """STP2 TFN declaration status.

    NEW_PAYEE_30D auto-flips to NOT_PROVIDED after 30 days — see
    Phase 2 cron design.
    """

    PROVIDED = "PROVIDED"
    NOT_PROVIDED = "NOT_PROVIDED"
    NEW_PAYEE_30D = "NEW_PAYEE_30D"
    EXEMPT_PENSIONER = "EXEMPT_PENSIONER"
    EXEMPT_UNDER_18 = "EXEMPT_UNDER_18"
    APPLIED_FOR = "APPLIED_FOR"


class EmploymentBasis(enum.StrEnum):
    """STP2 employment basis. Single letter on the wire."""

    FULL_TIME = "F"
    PART_TIME = "P"
    CASUAL = "C"
    LABOUR_HIRE = "L"
    VOLUNTARY_AGREEMENT = "V"
    NON_EMPLOYEE = "N"


class TerminationReason(enum.StrEnum):
    """STP2 cessation type. ATO Phase 2 valid set (no P; dropped in 2.0)."""

    VOLUNTARY = "V"
    ILL_HEALTH = "I"
    DECEASED = "D"
    REDUNDANCY = "R"
    DISMISSAL = "F"
    CONTRACT_CESSATION = "C"
    TRANSFER = "T"


class IncomeStreamType(enum.StrEnum):
    """STP2 income type."""

    SALARY_WAGES = "SAW"
    CLOSELY_HELD = "CHP"
    INBOUND_ASSIGNEE = "IAA"
    WORKING_HOLIDAY_MAKER = "WHM"
    SEASONAL_WORKER = "SWP"
    JOINT_PETROLEUM = "JPD"
    VOLUNTARY_AGREEMENT = "VOL"
    LABOUR_HIRE = "LAB"
    OTHER_SPECIFIED = "OSP"


class PayFrequency(enum.StrEnum):
    WEEKLY = "WEEKLY"
    FORTNIGHTLY = "FORTNIGHTLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUAL = "ANNUAL"


class PayBasis(enum.StrEnum):
    HOURLY = "HOURLY"
    SALARY = "SALARY"


class PayslipDelivery(enum.StrEnum):
    EMAIL = "EMAIL"
    PRINT = "PRINT"
    PORTAL = "PORTAL"


# --- Model -------------------------------------------------------------- #


class Employee(CompanyScoped, Base):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "employee_number",
            name="uq_employees_company_number",
        ),
        UniqueConstraint("contact_id", name="uq_employees_contact_id"),
        CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="ck_employees_end_after_start",
        ),
        CheckConstraint(
            "termination_reason IS NULL OR end_date IS NOT NULL",
            name="ck_employees_termination_needs_end_date",
        ),
        CheckConstraint(
            "working_holiday_maker = false OR whm_country_code IS NOT NULL",
            name="ck_employees_whm_country_required",
        ),
        CheckConstraint(
            "pay_basis = 'HOURLY' OR weekly_hours > 0",
            name="ck_employees_salary_needs_weekly_hours",
        ),
        CheckConstraint(
            "base_rate >= 0",
            name="ck_employees_base_rate_nonneg",
        ),
    )

    # --- identifiers --- #
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=lambda: _DEFAULT_TENANT_ID,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    employee_number: Mapped[str] = mapped_column(String(32), nullable=False)
    payee_id_bms: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4,
    )
    previous_payee_id: Mapped[str | None] = mapped_column(String(50))

    # --- sensitive (encrypted) --- #
    tfn_encrypted: Mapped[str | None] = mapped_column(Text)
    tfn_status: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in TfnStatus],
            name="tfn_status_enum",
            create_type=False,
        ),
        nullable=False,
        default=TfnStatus.NOT_PROVIDED.value,
        server_default=TfnStatus.NOT_PROVIDED.value,
    )

    # --- demographics --- #
    dob: Mapped[date | None] = mapped_column(Date)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    termination_reason: Mapped[str | None] = mapped_column(
        Enum(
            *[s.value for s in TerminationReason],
            name="termination_reason_enum",
            create_type=False,
        ),
    )

    # --- address (STP2 requires) --- #
    address_line1: Mapped[str | None] = mapped_column(String)
    address_line2: Mapped[str | None] = mapped_column(String)
    suburb: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str | None] = mapped_column(String(8))
    postcode: Mapped[str | None] = mapped_column(String(8))
    country_code: Mapped[str] = mapped_column(
        String(2), nullable=False, default="AU", server_default="AU",
    )

    # --- employment terms (STP2) --- #
    employment_basis: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in EmploymentBasis],
            name="employment_basis_enum",
            create_type=False,
        ),
        nullable=False,
    )
    tax_treatment_code: Mapped[str | None] = mapped_column(String(6))
    claims_tax_free_threshold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    is_australian_resident: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    study_training_support_loan: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    working_holiday_maker: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    whm_country_code: Mapped[str | None] = mapped_column(String(2))
    income_stream_type: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in IncomeStreamType],
            name="income_stream_type_enum",
            create_type=False,
        ),
        nullable=False,
        default=IncomeStreamType.SALARY_WAGES.value,
        server_default=IncomeStreamType.SALARY_WAGES.value,
    )
    payg_branch_code: Mapped[str | None] = mapped_column(String(3))

    # --- bank (encrypted; for net-pay disbursement) --- #
    bsb_encrypted: Mapped[str | None] = mapped_column(Text)
    account_number_encrypted: Mapped[str | None] = mapped_column(Text)
    account_name_encrypted: Mapped[str | None] = mapped_column(Text)

    # --- super --- #
    super_fund_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("super_funds.id", ondelete="RESTRICT"),
    )
    super_member_number: Mapped[str | None] = mapped_column(String(64))

    # --- contact (override of Contact) --- #
    payslip_email: Mapped[str | None] = mapped_column(String)
    payslip_delivery: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in PayslipDelivery],
            name="payslip_delivery_enum",
            create_type=False,
        ),
        nullable=False,
        default=PayslipDelivery.EMAIL.value,
        server_default=PayslipDelivery.EMAIL.value,
    )

    # --- pay shape --- #
    pay_frequency: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in PayFrequency],
            name="pay_frequency_enum",
            create_type=False,
        ),
        nullable=False,
        default=PayFrequency.WEEKLY.value,
        server_default=PayFrequency.WEEKLY.value,
    )
    pay_basis: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in PayBasis],
            name="pay_basis_enum",
            create_type=False,
        ),
        nullable=False,
        default=PayBasis.HOURLY.value,
        server_default=PayBasis.HOURLY.value,
    )
    base_rate: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False,
    )
    weekly_hours: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False,
        default=Decimal("38.00"), server_default="38.00",
    )

    # --- HR-only --- #
    notes: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # --- bookkeeping --- #
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
