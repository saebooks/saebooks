"""Concessional / non-concessional / TBC superannuation caps."""
import uuid
from decimal import Decimal

from sqlalchemy import Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class SuperContributionCap(ReferenceBase):
    __tablename__ = "super_contribution_caps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    fy_year: Mapped[int] = mapped_column(Integer, nullable=False)
    cap_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="concessional | non_concessional | transfer_balance | bring_forward",
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
