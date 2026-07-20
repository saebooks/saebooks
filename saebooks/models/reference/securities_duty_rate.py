"""Securities-transfer duty / financial-transaction tax rates (M1.5 · 5-DUTIES).

A per-jurisdiction catalog of duty on the transfer of shares/units
(marketable securities duty, UK-style stamp duty on shares, FTT-style
levies). Deliberately a SIBLING of ``duty_rate_schedules``, not merged
into it (per the audit): securities duty keys on the class of security,
not a bracketed dutiable-value range.

Rows are effective-dated; ``effective_to`` NULL = still in force. The
AU seed rows are all CLOSED (``effective_to`` set) — Australia abolished
marketable-securities duty (quoted nationally from 2001, unquoted
state-by-state, last in NSW from 1 July 2016), so AU parity is "no open
row", and recording a historical or foreign securities transfer uses a
``DutiableTransactionEvent`` with the existing ``duty_type='securities'``.
"""
import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class SecurityClass(enum.StrEnum):
    """Class of security the rate row covers."""

    QUOTED = "quoted"      # listed / quoted on a recognised exchange
    UNQUOTED = "unquoted"  # unlisted shares/units
    ALL = "all"


SECURITY_CLASSES = tuple(c.value for c in SecurityClass)


class RefSecuritiesDutyRate(ReferenceBase):
    """One securities-transfer duty rate for one security class in one
    (sub-)jurisdiction for a dated period."""

    __tablename__ = "securities_duty_rates"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "sub_jurisdiction", "security_class", "effective_from",
            name="uq_securities_duty_rates_natkey",
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
    security_class: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="One of SECURITY_CLASSES.",
    )
    rate: Mapped[Decimal] = mapped_column(
        Numeric(7, 4),
        nullable=False,
        comment="Percentage of the dutiable base (0.6000 = 0.6%, i.e. 60c per $100).",
    )
    rate_basis: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="consideration",
        comment="consideration | market_value (the higher-of base most acts use).",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(
        Date, comment="NULL = still in force."
    )
    description: Mapped[str | None] = mapped_column(String(512))
