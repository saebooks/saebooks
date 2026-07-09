"""ATO TR 2024/x effective life rulings (and equivalents in NZ/UK/EE)."""
import uuid

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class DepreciationEffectiveLife(ReferenceBase):
    __tablename__ = "depreciation_effective_lives"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    asset_class: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_subclass: Mapped[str | None] = mapped_column(String(128))
    effective_life_years: Mapped[float] = mapped_column(
        Numeric(6, 2), nullable=False
    )
    source_ruling: Mapped[str | None] = mapped_column(
        String(64),
        comment="ATO ruling reference, e.g. TR 2024/1",
    )
