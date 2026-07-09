"""Canonical per-jurisdiction wage/other withholding tables (M1.5 · T7).

AU payroll withholding lives under an AU noun — ``payg_withholding_scales``,
shaped exactly like the ATO's own a/b-coefficient formula (``PaygWithholdingScale``,
see ``payg_withholding_scale.py``). That shape does not fit most other
jurisdictions: the US federal wage-bracket method is a lookup table, UK PAYE
is bracketed like an income-tax scale, and dividend/interest/royalty
withholding is usually a single flat rate. Rather than force every country
into the ATO's coefficient shape, this table stores the withholding rule
itself as data: a ``formula_type`` tag plus a free-shape ``parameters``
JSONB blob whose keys are defined by that formula type (services interpret
``parameters`` according to ``formula_type`` — there is no cross-jurisdiction
schema for it, deliberately, since the shapes genuinely differ).

Additive only — ``payg_withholding_scales`` is untouched; a future pass may
migrate AU onto this table and retire the AU-specific one, but that is a
deferred, coordinated rename, not part of this change.

See docs/multi-jurisdiction.md (M1.5) (theme T7).
"""
import enum
import uuid
from datetime import date
from typing import Any

from sqlalchemy import Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class WithholdingType(enum.StrEnum):
    """What kind of payment the withholding is taken from."""

    WAGE_PAYE = "wage_paye"          # employment income (PAYG/PAYE/federal withholding)
    DIVIDEND = "dividend"
    INTEREST = "interest"
    ROYALTY = "royalty"
    NON_RESIDENT = "non_resident"    # catch-all non-resident withholding tax


class FormulaType(enum.StrEnum):
    """How ``WithholdingTable.parameters`` should be interpreted."""

    BRACKETED = "bracketed"          # tiered brackets, each with its own rate
    FLAT_RATE = "flat_rate"          # single rate applied to the whole amount
    COEFFICIENT = "coefficient"      # ATO-style a*x - b linear coefficients
    LOOKUP_TABLE = "lookup_table"    # literal wage-bracket lookup rows


class WithholdingTable(ReferenceBase):
    """A named withholding rule in one jurisdiction, active over a date
    range. NOT per-company — payroll services pick a code from here."""

    __tablename__ = "withholding_tables"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "code", "effective_from",
            name="uq_withholding_tables_jur_code_eff",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Stable per-jurisdiction code, e.g. 'au_payg_scale2', 'us_fed_2024', 'uk_paye'.",
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    withholding_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=WithholdingType.WAGE_PAYE.value,
        comment="One of WithholdingType — what kind of payment this withholds from.",
    )
    formula_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of FormulaType — how to interpret 'parameters'.",
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Bracket/coefficient/lookup data; shape depends on formula_type.",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
