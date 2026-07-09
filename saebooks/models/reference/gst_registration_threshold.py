"""GST/VAT registration turnover thresholds per jurisdiction."""
import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class GstRegistrationThreshold(ReferenceBase):
    __tablename__ = "gst_registration_threshold"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    fy_year: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    applies_to: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="standard | non_profit | taxi_ride_share",
    )
