"""Medicare levy thresholds and surcharge brackets."""
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class MedicareLevy(ReferenceBase):
    __tablename__ = "medicare_levy"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    fy_year: Mapped[int] = mapped_column(Integer, nullable=False)
    taxpayer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold_no_levy: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    threshold_full_levy: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    surcharge_brackets: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
