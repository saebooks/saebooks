"""Per-jurisdiction income tax brackets by FY and taxpayer type."""
import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class IncomeTaxBracket(ReferenceBase):
    __tablename__ = "income_tax_brackets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    fy_year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="FY end year, e.g. 2026 for FY2025-26",
    )
    taxpayer_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="resident_individual | non_resident_individual | working_holiday | minor | company",
    )
    lower_bound: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    upper_bound: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
