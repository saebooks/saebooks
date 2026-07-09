"""Fuel Tax Credit cents-per-litre by fuel/vehicle category and period."""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class FuelTaxCreditRate(ReferenceBase):
    __tablename__ = "fuel_tax_credit_rates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    fuel_type: Mapped[str] = mapped_column(String(64), nullable=False)
    vehicle_type: Mapped[str] = mapped_column(String(64), nullable=False)
    rate_cents_per_litre: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
