"""Stamp / transfer duty rates by jurisdiction, state and transaction type."""
import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class StampDutyRate(ReferenceBase):
    __tablename__ = "stamp_duty_rates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(8), nullable=False)
    transaction_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="property_transfer | motor_vehicle | insurance | mortgage",
    )
    lower_bound: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    upper_bound: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
