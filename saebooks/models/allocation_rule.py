"""AllocationRule model — overhead allocation engine.

An allocation rule captures how a shared cost pool (the source account)
should be split across target accounts. Running the rule generates a
balanced journal entry that debits each target for its share and
credits the source.

``targets`` is a JSONB array of objects::

    [
      {"account_id": "<uuid>", "label": "Site A Rent", "percentage": 20.0},
      ...
    ]

The percentages must sum to 100 (validated in the service layer before
any JE is generated).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class AllocationRule(CompanyScoped, Base):
    __tablename__ = "allocation_rules"

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
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    source_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Array of {account_id, label, percentage} objects
    targets: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
