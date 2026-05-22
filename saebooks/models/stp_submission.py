"""STP Phase 2 submission record.

One row per assembled payload. Lifecycle:

    READY → SUBMITTED → (ACCEPTED | REJECTED) → SUPERSEDED?

The actual submit-to-ATO logic lands in Phase 3.1 — for now we just
build and store the payload so the operator can see "STP-ready" and
inspect what would be sent.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class StpEventType(enum.StrEnum):
    PAY = "PAY"
    UPDATE = "UPDATE"
    FINALISATION = "FINALISATION"


class StpStatus(enum.StrEnum):
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class StpSubmission(CompanyScoped, Base):
    __tablename__ = "stp_submissions"

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
        default=lambda: _DEFAULT_TENANT_ID,
    )
    pay_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pay_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in StpEventType],
            name="stp_event_type_enum",
            create_type=False,
        ),
        nullable=False,
        default=StpEventType.PAY.value,
    )
    status: Mapped[str] = mapped_column(
        Enum(
            *[s.value for s in StpStatus],
            name="stp_status_enum",
            create_type=False,
        ),
        nullable=False,
        default=StpStatus.READY.value,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    ato_receipt_number: Mapped[str | None] = mapped_column(String(64))
    ato_response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("stp_submissions.id", ondelete="SET NULL"),
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
