"""Per-jurisdiction dividend relief mechanisms (M1.5 · T11).

Before this table the engine had no representation of how a jurisdiction
relieves double taxation of company profits distributed as dividends — AU
franking credits (refundable, imputation-family), a classical system with
no relief at all, a partial-credit system, or a one-tier exemption system.

This mirrors ``RefTaxCode``: a per-jurisdiction reference table, active
over a date range (the spec that named this table listed no explicit
effective-dating field, but dividend regimes genuinely change over time —
e.g. AU's own move from classical to imputation in 1987 — and this is a
brand-new table, so adding it is additive and keeps this table consistent
with every other reference table in this package. See the M1.5 build
notes for this call-out).

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T11,
domain "Income, corporate & capital taxes").
"""
import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy import (
    false as sa_false,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class DividendReliefMechanismType(enum.StrEnum):
    """Jurisdiction-neutral dividend-relief mechanisms. Every local
    dividend-tax regime resolves to exactly one of these."""

    IMPUTATION = "imputation"          # tax paid at company level imputed to shareholder
    FRANKING = "franking"              # AU-specific imputation variant (franking credits)
    PARTIAL_CREDIT = "partial_credit"  # shareholder gets a partial credit, not full imputation
    EXEMPTION = "exemption"            # dividend wholly or largely exempt (one-tier systems)
    CLASSICAL = "classical"            # no relief — dividend fully taxed again at shareholder level


DIVIDEND_RELIEF_MECHANISM_TYPES = tuple(m.value for m in DividendReliefMechanismType)


class DividendReliefMechanism(ReferenceBase):
    """A dividend-relief mechanism in force in one jurisdiction over a
    date range. NOT per-company — dividend/distribution services pick a
    row from here keyed by the paying company's jurisdiction."""

    __tablename__ = "dividend_relief_mechanisms"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "mechanism_type", "effective_from",
            name="uq_dividend_relief_mechanisms_jur_type_eff",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    mechanism_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="One of DIVIDEND_RELIEF_MECHANISM_TYPES.",
    )
    credit_or_exemption_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 4),
        comment=(
            "For imputation/franking/partial_credit: the credit rate as a "
            "percentage (30.0000 = 30%, the AU corporate rate that backs a "
            "fully-franked credit). For exemption: the exempt percentage. "
            "NULL for classical (no relief)."
        ),
    )
    refundable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa_false(),
        comment="True if excess credit is refundable to the shareholder (AU franking); false if it can only offset tax payable.",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
