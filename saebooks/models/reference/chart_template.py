"""Recommended chart of accounts per jurisdiction (used at company creation)."""
import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class ChartTemplate(ReferenceBase):
    __tablename__ = "chart_template"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "account_code",
            name="uq_chart_template_jur_code",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    account_code: Mapped[str] = mapped_column(String(32), nullable=False)
    account_name: Mapped[str] = mapped_column(String(128), nullable=False)
    account_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="ASSET | LIABILITY | EQUITY | INCOME | EXPENSE | COST_OF_SALES | OTHER_INCOME | OTHER_EXPENSE",
    )
    default_tax_code: Mapped[str | None] = mapped_column(String(32))
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
