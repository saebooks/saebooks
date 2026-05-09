"""ATO General Interest Charge / Shortfall Interest Charge / Late Payment Interest."""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class AtoInterestRate(ReferenceBase):
    __tablename__ = "ato_interest_rates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quarter_start: Mapped[date] = mapped_column(Date, nullable=False)
    quarter_end: Mapped[date] = mapped_column(Date, nullable=False)
    gic_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    sic_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    lpi_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
