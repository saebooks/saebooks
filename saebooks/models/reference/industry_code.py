"""ANZSIC / NZSIC / SIC 2007 / NACE industry classification codes."""
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class IndustryCode(ReferenceBase):
    __tablename__ = "industry_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    code_system: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="ANZSIC | NZSIC | SIC2007 | NACE",
    )
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    parent_code: Mapped[str | None] = mapped_column(String(16))
