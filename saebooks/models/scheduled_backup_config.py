"""Per-tenant scheduled-backup configuration (planned-modules Wave E,
FLAG_SCHEDULED_BACKUPS).

Tenant-scoped, NOT company-scoped — a backup covers the whole tenant's
per-tenant logical export (see ``services/backup_export.py``), matching
Richard's decision 6 ("per-tenant LOGICAL export"). Mirrors
``models/inbox_document.py``'s tenant-only shape (tenant_id column,
ENABLE+FORCE RLS + tenant_isolation policy, migration 0185) — no
``company_id`` and therefore no tenant-coherence trigger is needed
here (there is no child FK to a company row to keep coherent).

``destination_params`` NEVER stores a secret value. For
``destination_type="rclone_remote"`` it stores the rclone remote NAME
(a reference into rclone's own config, e.g. ``{"remote": "backblaze",
"path": "/sae-backups/<tenant>"}``) — the actual remote credentials
live in rclone's own encrypted config on the host, never in this row.
For ``destination_type="local_path"`` it stores a path relative to
``settings.scheduled_backup_local_dest_root`` (validated at write time
so a tenant config can never point outside that root).

``managed_by`` is the liability-pricing extension point
([[saebooks-liability-pricing-principle]]): ``"client"`` (the only
value the service layer currently implements) means the client
supplies their own passphrase and owns the destination/retention —
open baseline, no SAE liability assumed. ``"sae"`` is RESERVED for a
future SAE-managed-certificate / SAE-guaranteed-handling tier (the
priced path — SAE assuming custody risk, per the pricing principle's
"the fee compensates the risk SAE absorbs"). The column and CHECK
constraint exist now so the schema doesn't need a migration to add the
option later; the service layer rejects ``"sae"`` at trigger-time with
a clear "not implemented" error (see ``services/scheduled_backups.py``)
rather than silently accepting a liability commitment nobody built.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base

DESTINATION_TYPES = ("local_path", "rclone_remote")
MANAGED_BY_VALUES = ("client", "sae")


class ScheduledBackupConfig(Base):
    __tablename__ = "scheduled_backup_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    destination_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="local_path", server_default="local_path"
    )
    # See module docstring — NEVER a secret value, only references.
    destination_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Liability-pricing extension point — see module docstring.
    # "client" is the only value the service layer implements today.
    managed_by: Mapped[str] = mapped_column(
        String(16), nullable=False, default="client", server_default="client"
    )
    retention_keep_n: Mapped[int | None] = mapped_column(
        Integer, comment="Keep the N most recent successful runs; NULL = unbounded"
    )
    retention_keep_days: Mapped[int | None] = mapped_column(
        Integer, comment="Keep runs newer than this many days; NULL = unbounded"
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
