"""Tenant model — the top-level isolation boundary for multi-tenant deployments.

Community edition: one tenant (Default, id 00000000-0000-0000-0000-000000000001).
Business/Pro/Enterprise: one tenant per customer or organisational unit.

All entity tables carry a ``tenant_id`` FK.  Postgres RLS policies on each
entity table enforce isolation at the database layer via the
``app.current_tenant`` session-local variable set by the API middleware.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
