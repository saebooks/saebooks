"""Conditional tax rules expressed in a bounded JSONB DSL."""
import uuid
from datetime import date
from typing import Any

from sqlalchemy import Date, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class TaxRule(ReferenceBase):
    """A rule like 'export sale → force GST_FREE'.

    The DSL is intentionally small; see docs/multi-jurisdiction.md for
    the closed vocabulary. Anything that cannot be expressed in the DSL
    falls to a Strategy class instead.
    """

    __tablename__ = "tax_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    rule_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="default_tax_code | force_tax_code | warn | block",
    )
    applies_to: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="invoice_line | bill_line | journal_line | any",
    )
    condition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
