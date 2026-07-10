"""One row per scheduled-backup export attempt (planned-modules Wave E,
FLAG_SCHEDULED_BACKUPS). Tenant-scoped, same shape as
``models/scheduled_backup_config.py`` — see that module's docstring for
the RLS/tenant-only rationale.

What's stored here is metadata about an export, NEVER the plaintext
export and NEVER the client's passphrase:

* ``artifact_path`` points at a CIPHERTEXT file on local staging disk
  (``settings.scheduled_backup_export_dir``) — the encrypted envelope
  produced by ``services/backup_crypto.encrypt_export``. The passphrase
  that encrypted it is supplied by the caller at trigger time and is
  never written to this row, to disk outside the ciphertext, to
  ``change_log``, or to a log line (see
  ``services/scheduled_backups.py`` docstring). SAE Books cannot
  decrypt this artifact after this row is written — that is the
  point of the client-managed-passphrase model.
* ``artifact_sha256`` is the SHA-256 of the CIPHERTEXT (integrity
  check for "did the file get corrupted in transit/at rest" — it is
  NOT a decryption aid and reveals nothing about the plaintext).
* ``table_counts`` is the export manifest's per-table row counts only
  (``TenantExportResult.to_manifest_dict()``["tables"]) — safe summary
  metadata about the tenant's own data, visible only to that tenant's
  own admin.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CHAR, BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base

RUN_STATUSES = ("PENDING", "RUNNING", "SUCCESS", "FAILED")
REMOTE_PUSH_STATUSES = (
    "not_applicable",
    "stubbed_not_implemented",
    "pending",
    "success",
    "failed",
)


class ScheduledBackupRun(Base):
    __tablename__ = "scheduled_backup_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Ad-hoc trigger runs (no saved config, destination = download-only)
    # have NULL here. A deleted config SET NULLs rather than orphaning
    # or deleting historical run rows.
    config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scheduled_backup_configs.id", ondelete="SET NULL"),
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default="PENDING"
    )
    # Snapshot of the destination type used for THIS run (independent of
    # whatever the config says now) — 'download_only' when triggered
    # ad-hoc with no destination push.
    destination_type: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    artifact_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    artifact_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    table_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Remote-push extension point (services/backup_destinations.py) —
    # 'stubbed_not_implemented' for rclone_remote today; see that
    # module's docstring.
    remote_push_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="not_applicable",
        server_default="not_applicable",
    )
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
