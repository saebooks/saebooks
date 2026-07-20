"""State / sub-jurisdiction payroll tax thresholds and rates."""
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class PayrollTaxRate(ReferenceBase):
    __tablename__ = "payroll_tax_rates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="State / region code (e.g. QLD, NSW, ENG, SCT)",
    )
    # M1.5 · 5-SUBJURIS (reference migration 0016): FK promotion of the
    # ad-hoc ``state`` string into the T3 jurisdiction tree. NULLABLE and
    # additive — ``state`` stays authoritative for existing callers during
    # the transition; AU rows are backfilled ('QLD' → 'AU-QLD').
    sub_jurisdiction_code: Mapped[str | None] = mapped_column(
        String(6),
        ForeignKey("jurisdictions.code"),
        comment="Sub-national jurisdiction node (T3 tree), e.g. 'AU-QLD'.",
    )
    fy_year: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    deduction_formula: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
