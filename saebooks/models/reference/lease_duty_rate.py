"""Lease / tenancy agreement duty rates (M1.5 · 5-DUTIES).

A per-jurisdiction catalog of duty on the grant of a lease, keyed on the
dutiable base (``rent_reserved`` — the historical rent-based lease duty
— or ``premium`` — an up-front lease premium taxed like consideration).
Sibling of ``duty_rate_schedules`` (per the audit's Duties domain):
lease duty is rate-on-base, not a bracketed value schedule.

Rows are effective-dated; ``effective_to`` NULL = still in force. The
AU seed rows are all CLOSED — Australian states abolished rent-based
lease duty in the 2000s (QLD from 1 Jan 2006, NSW from 1 Jan 2008), and
a lease PREMIUM today is dutiable as ordinary transfer consideration —
so AU parity is "no open row". A jurisdiction that still levies lease
duty seeds an open row; the postable record is a
``DutiableTransactionEvent`` with ``duty_type='lease'``.
"""
import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class LeaseDutyBase(enum.StrEnum):
    """What the lease duty rate applies to."""

    RENT_RESERVED = "rent_reserved"  # total/annual rent reserved by the lease
    PREMIUM = "premium"              # up-front premium / fine paid for the grant


LEASE_DUTY_BASES = tuple(b.value for b in LeaseDutyBase)


class RefLeaseDutyRate(ReferenceBase):
    """One lease-duty rate for one dutiable base in one (sub-)jurisdiction
    for a dated period."""

    __tablename__ = "lease_duty_rates"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "sub_jurisdiction", "duty_base", "effective_from",
            name="uq_lease_duty_rates_natkey",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    sub_jurisdiction: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment=(
            "Same vocabulary as duty_rate_schedules.state ('QLD', 'NSW', "
            "...); 'ALL' for a country-wide duty."
        ),
    )
    duty_base: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of LEASE_DUTY_BASES.",
    )
    rate: Mapped[Decimal] = mapped_column(
        Numeric(7, 4),
        nullable=False,
        comment="Percentage of the dutiable base (0.3500 = 0.35%, i.e. 35c per $100).",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(
        Date, comment="NULL = still in force."
    )
    description: Mapped[str | None] = mapped_column(String(512))
