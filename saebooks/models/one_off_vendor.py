"""One-off vendor — lightweight party record for COD / walk-in / single-purchase suppliers.

Lives outside the contacts table so the contact list stays clean (Richard,
2026-05-26: "I want to record the vendor, but this should technically not
be a contact"). Carries enough columns to post an expense or bill against
a recognisable name plus a default GL account + tax code, but no
addresses, no bank details, no TPAR — those belong to ongoing contacts.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class OneOffVendor(CompanyScoped, Base):
    __tablename__ = "one_off_vendors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    abn: Mapped[str | None] = mapped_column(String(14))
    default_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    default_tax_code: Mapped[str | None] = mapped_column(String(16))
    notes: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_spent: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_to_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
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
