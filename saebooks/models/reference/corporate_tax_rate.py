"""Per-jurisdiction corporate income tax rates (M1.5 · T11).

Before this table the only place a "company" tax rate lived was
``IncomeTaxBracket.taxpayer_type == 'company'`` — a single flat bracket
row with no notion of a turnover-tiered scope (AU's base-rate-entity vs
standard rate), no sub-jurisdiction layer (US state corporate tax, German
Gewerbesteuer), and no FY-scoped effective dating separate from the
personal-income bracket table.

This mirrors ``RefTaxCode``: a per-jurisdiction reference table, keyed by
jurisdiction + sub_jurisdiction + tax_year + entity_scope, active over a
date range. Full turnover-threshold/marginal-relief/Pillar-Two modelling
(the audit's fuller proposal) is deferred — this is the M1.5 Wave 2 slice:
a rate lookup by scope, not yet a formula engine.

See docs/multi-jurisdiction.md (M1.5) (theme T11,
domain "Income, corporate & capital taxes").
"""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class CorporateTaxRate(ReferenceBase):
    """A corporate income tax rate in force for one jurisdiction / scope /
    tax year. NOT per-company — income-tax services pick a row from here
    keyed by the company's jurisdiction and entity scope."""

    __tablename__ = "corporate_tax_rates"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "sub_jurisdiction", "tax_year", "entity_scope",
            name="uq_corporate_tax_rates_jur_subjur_year_scope",
            # sub_jurisdiction is nullable (most rows are national-only), and
            # under default Postgres semantics NULL never conflicts with
            # NULL, which would make the seed loader's ON CONFLICT upsert
            # silently no-op into duplicate inserts instead. Treat NULLs as
            # equal for this constraint so re-loading a national-only row
            # (sub_jurisdiction=NULL) stays idempotent. Requires Postgres 15+
            # (this project runs postgres:16).
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    sub_jurisdiction: Mapped[str | None] = mapped_column(
        String(8),
        comment="State/province/city code layering on top of jurisdiction (e.g. a US state). NULL = national rate only.",
    )
    tax_year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="FY end year, e.g. 2026 for FY2025-26.",
    )
    entity_scope: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Free-text scope key, e.g. 'base_rate_entity', 'standard'.",
    )
    rate_percent: Mapped[Decimal] = mapped_column(
        Numeric(7, 4),
        nullable=False,
        comment="Rate as a percentage (25.0000 = 25%, not 0.25).",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
