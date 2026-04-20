"""Per-company sequential document counter.

One row per (company_id, kind) where kind is the document class —
``invoice``, ``bill``, ``credit_note``, ``payment``, ``quote``. The
``services/numbering.py`` module advances ``next_value`` atomically
under a SELECT ... FOR UPDATE so concurrent requests can't skip a
number. Gap-free numbering is a hard AU ATO requirement for tax
invoices.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class DocumentCounter(Base):
    __tablename__ = "document_counters"
    __table_args__ = (
        UniqueConstraint("company_id", "kind", name="uq_document_counters_company_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    next_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    pad_width: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
