"""Tax offsets / rebates (LITO, SAPTO, etc.)."""
import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class TaxOffset(ReferenceBase):
    __tablename__ = "tax_offsets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    fy_year: Mapped[int] = mapped_column(Integer, nullable=False)
    offset_code: Mapped[str] = mapped_column(String(32), nullable=False)
    max_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    lower_threshold: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    upper_threshold: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    taper_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
