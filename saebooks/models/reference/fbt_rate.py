"""Fringe Benefits Tax rates and gross-up factors."""
import uuid
from decimal import Decimal

from sqlalchemy import Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class FbtRate(ReferenceBase):
    __tablename__ = "fbt_rates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    fy_year: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    fbt_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    type1_gross_up: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    type2_gross_up: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    statutory_interest_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    car_parking_threshold: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
