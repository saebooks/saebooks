"""ATO PAYG withholding coefficients for the standard a/b formula."""
import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class PaygWithholdingScale(ReferenceBase):
    __tablename__ = "payg_withholding_scales"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    fy_year: Mapped[int] = mapped_column(Integer, nullable=False)
    scale_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="ATO scale 1-9 (TFN status / rebate / Medicare combinations)",
    )
    weekly_earnings_lower: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    weekly_earnings_upper: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    a_coefficient: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    b_subtractor: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
