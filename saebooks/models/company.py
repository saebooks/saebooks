import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String)
    trading_name: Mapped[str | None] = mapped_column(String)
    abn: Mapped[str | None] = mapped_column(String(20))
    acn: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    base_currency: Mapped[str] = mapped_column(String(3), default="AUD", nullable=False)
    fin_year_start_month: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    audit_mode: Mapped[str] = mapped_column(String, default="immutable", nullable=False)

    # Per-company SISS credentials (Batch II, Enterprise-gated via
    # FLAG_PER_COMPANY_SISS). NULL on any field = fall back to env-var
    # creds (pre-Batch-II behaviour). ``*_encrypted`` columns are Fernet
    # ciphertext produced by ``saebooks.services.crypto.encrypt_field`` —
    # never persist plaintext here. ``siss_environment`` is free-text
    # (``production`` / ``sandbox``) routed by the resolver.
    siss_client_id: Mapped[str | None] = mapped_column(String(128))
    siss_client_secret_encrypted: Mapped[str | None] = mapped_column(String)
    siss_subscription_key_encrypted: Mapped[str | None] = mapped_column(String)
    siss_environment: Mapped[str | None] = mapped_column(String(32))

    # Optimistic-locking version — bumped on every write through the API.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
