"""Australian Superannuation Guarantee rate by FY."""
import uuid
from decimal import Decimal

from sqlalchemy import Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class SuperGuaranteeRate(ReferenceBase):
    __tablename__ = "super_guarantee_rates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    fy_year: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
