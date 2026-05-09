"""Receipt of a transmission attempt by lodge-server."""
import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class LodgementStatus(enum.StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"


class LodgementRecord(CompanyScoped, Base):
    """Lodgement transmission record.

    Written by the lodge-server callback path. Request and response
    blobs are JSONB so we can store the rendered envelope and the raw
    regulator response without designing a fixed schema for every
    protocol; secrets MUST be redacted before persistence.
    """

    __tablename__ = "lodgement_records"

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
    tax_return_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tax_returns.id", ondelete="RESTRICT"),
        nullable=False,
    )
    jurisdiction: Mapped[str] = mapped_column(String(3), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    regulator: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="ATO | IRD | HMRC | MTA | OSS-EE | …",
    )
    regulator_reference: Mapped[str | None] = mapped_column(
        String(128),
        comment="Receipt ID, correlation ID, etc.",
    )
    status: Mapped[LodgementStatus] = mapped_column(
        Enum(
            LodgementStatus,
            name="lodgement_status",
            values_callable=lambda et: [e.value for e in et],
        ),
        nullable=False,
        default=LodgementStatus.PENDING,
    )
    request_blob: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response_blob: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
