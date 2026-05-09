"""A generated return — figures, status, optional lodgement link."""
import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class TaxReturnStatus(enum.StrEnum):
    DRAFT = "draft"
    READY = "ready"
    LODGED = "lodged"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AMENDED = "amended"


class TaxReturn(CompanyScoped, Base):
    __tablename__ = "tax_returns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    jurisdiction: Mapped[str] = mapped_column(String(3), nullable=False)
    period_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tax_periods.id", ondelete="RESTRICT"),
        nullable=False,
    )
    return_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="BAS | IAS | GST101 | VAT100 | KMD | KMD-INF | INF-EU | OSS-Q",
    )
    figures: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    generated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[TaxReturnStatus] = mapped_column(
        Enum(
            TaxReturnStatus,
            name="tax_return_status",
            values_callable=lambda et: [e.value for e in et],
        ),
        nullable=False,
        default=TaxReturnStatus.DRAFT,
    )
    lodgement_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lodgement_records.id", ondelete="SET NULL")
    )
