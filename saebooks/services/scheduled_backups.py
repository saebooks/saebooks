"""Scheduled backups orchestration (planned-modules Wave E,
FLAG_SCHEDULED_BACKUPS) — config CRUD, triggering an export, retention,
and reading an artifact back for download.

Ties together three lower-level modules, each independently testable:

* ``services/backup_export.py`` — the per-tenant LOGICAL export (the
  data-isolation-critical part: proven to contain zero foreign-tenant
  rows).
* ``services/backup_crypto.py`` — client-passphrase envelope
  encryption. The passphrase argument to :func:`trigger_export` is used
  exactly once, to encrypt, and is never written to ``ScheduledBackupRun``,
  ``change_log``, or a log line — grep this file: there is no
  ``passphrase`` reference anywhere below the point it's consumed by
  ``backup_crypto.encrypt_export``.
* ``services/backup_destinations.py`` — the local-path (real) / rclone
  (stubbed) push after the ciphertext is staged.

Passphrase handling — the liability boundary made concrete
------------------------------------------------------------
SAE Books encrypts the export with the caller-supplied passphrase and
IMMEDIATELY writes only ciphertext to disk. The plaintext export
(built in ``backup_export.export_tenant_data``) and the passphrase
string both go out of scope at the end of :func:`trigger_export` and
are never persisted anywhere — SAE Books is structurally unable to
decrypt a tenant's own export after this function returns. This is
decision 6's "encrypted on download is the LIMIT of SAE's
responsibility" made literal: not a policy promise, an architectural
fact (see ``services/backup_crypto.py`` module docstring).

Storage layout
--------------
Ciphertext artifacts stage under ``settings.scheduled_backup_export_dir``
(default ``/app/scheduled-backups``), one subdirectory per tenant:
``<export_dir>/<tenant_id>/<run_id>.enc``. This is DELIBERATELY separate
from ``services/backups.py``'s ``SAEBOOKS_BACKUP_DIR_IN_CONTAINER``
(the infra whole-DB ``pg_dump`` timer's output) — different writer
(this module vs. a systemd timer), different retention policy
(per-tenant config vs. a fixed ops schedule), different content
(per-tenant ciphertext vs. a single whole-DB dump that must never be
exposed to a tenant at all).
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.config import Settings
from saebooks.config import settings as _default_settings
from saebooks.models.scheduled_backup_config import (
    DESTINATION_TYPES,
    MANAGED_BY_VALUES,
    ScheduledBackupConfig,
)
from saebooks.models.scheduled_backup_run import ScheduledBackupRun
from saebooks.services import backup_crypto
from saebooks.services.backup_destinations import (
    DestinationConfigError,
    RemotePushNotImplementedError,
    get_destination,
)
from saebooks.services.backup_export import (
    export_tenant_data,
    gzip_json,
)

_log = logging.getLogger("saebooks.scheduled_backups")


class ManagedBySaeNotImplementedError(ValueError):
    """Raised when a config requests ``managed_by='sae'`` — the
    SAE-managed-certificate / SAE-guaranteed-handling tier is a
    reserved extension point (see model docstring), not built. This is
    a DELIBERATE refusal, not a bug: accepting the request would imply
    SAE assumed a liability nobody has priced or built handling for.
    """


class BackupConfigNotFoundError(LookupError):
    pass


class BackupRunNotFoundError(LookupError):
    pass


def _staging_dir(tenant_id: uuid.UUID, settings: Settings) -> Path:
    return Path(settings.scheduled_backup_export_dir) / str(tenant_id)


def _artifact_path(tenant_id: uuid.UUID, run_id: uuid.UUID, settings: Settings) -> Path:
    return _staging_dir(tenant_id, settings) / f"{run_id}.enc"


# --------------------------------------------------------------------- #
# Config CRUD                                                            #
# --------------------------------------------------------------------- #


async def get_config(
    session: AsyncSession, tenant_id: uuid.UUID
) -> ScheduledBackupConfig | None:
    result = await session.execute(
        select(ScheduledBackupConfig).where(
            ScheduledBackupConfig.tenant_id == tenant_id
        )
    )
    return result.scalars().first()


def _validate_config_shape(
    destination_type: str, destination_params: dict[str, Any], managed_by: str
) -> None:
    if destination_type not in DESTINATION_TYPES:
        raise DestinationConfigError(
            f"destination_type must be one of {DESTINATION_TYPES}, got {destination_type!r}"
        )
    if managed_by not in MANAGED_BY_VALUES:
        raise ValueError(f"managed_by must be one of {MANAGED_BY_VALUES}, got {managed_by!r}")
    if managed_by == "sae":
        raise ManagedBySaeNotImplementedError(
            "managed_by='sae' (SAE-managed certificates / SAE-guaranteed "
            "handling) is a reserved extension point, not implemented in "
            "this build. Use managed_by='client' (the open, self-managed "
            "baseline) — you supply your own passphrase and own the "
            "destination/retention; see decision 6 in "
            "planned-modules-build-plan.md."
        )
    # Shape-validates (and, for local_path, root-containment-validates)
    # without performing any I/O.
    get_destination(destination_type).validate(destination_params)


async def upsert_config(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    enabled: bool,
    destination_type: str,
    destination_params: dict[str, Any],
    retention_keep_n: int | None,
    retention_keep_days: int | None,
    managed_by: str,
    user_id: uuid.UUID | None,
) -> ScheduledBackupConfig:
    _validate_config_shape(destination_type, destination_params, managed_by)

    existing = await get_config(session, tenant_id)
    if existing is None:
        existing = ScheduledBackupConfig(tenant_id=tenant_id, created_by=user_id)
        session.add(existing)

    existing.enabled = enabled
    existing.destination_type = destination_type
    existing.destination_params = destination_params
    existing.retention_keep_n = retention_keep_n
    existing.retention_keep_days = retention_keep_days
    existing.managed_by = managed_by
    existing.updated_by = user_id

    await session.flush()
    return existing


# --------------------------------------------------------------------- #
# Runs — list / get                                                      #
# --------------------------------------------------------------------- #


async def list_runs(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int = 50, offset: int = 0
) -> list[ScheduledBackupRun]:
    result = await session.execute(
        select(ScheduledBackupRun)
        .where(ScheduledBackupRun.tenant_id == tenant_id)
        .order_by(ScheduledBackupRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def get_run(
    session: AsyncSession, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> ScheduledBackupRun | None:
    result = await session.execute(
        select(ScheduledBackupRun).where(
            ScheduledBackupRun.id == run_id,
            ScheduledBackupRun.tenant_id == tenant_id,
        )
    )
    return result.scalars().first()


# --------------------------------------------------------------------- #
# Trigger export                                                         #
# --------------------------------------------------------------------- #


async def trigger_export(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    passphrase: str,
    user_id: uuid.UUID | None,
    settings: Settings | None = None,
) -> ScheduledBackupRun:
    """Run one full export → encrypt → stage → (maybe) push → retain cycle.

    ``passphrase`` is consumed exactly once, by
    ``backup_crypto.encrypt_export`` a few lines below. Nothing after
    that point in this function (or anywhere else in the codebase)
    reads it again. Weak-passphrase and destination-validation errors
    are raised BEFORE any export work happens, and before the run row's
    status leaves PENDING, so a bad request never leaves behind a
    partially-built artifact.
    """
    effective_settings = settings if settings is not None else _default_settings
    backup_crypto.validate_passphrase_strength(passphrase)

    config = await get_config(session, tenant_id)
    destination_type = "download_only"
    if config is not None and config.enabled:
        destination_type = config.destination_type
        _validate_config_shape(
            config.destination_type, config.destination_params, config.managed_by
        )

    run = ScheduledBackupRun(
        tenant_id=tenant_id,
        config_id=config.id if config is not None else None,
        status="RUNNING",
        destination_type=destination_type,
        requested_by=user_id,
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()  # assigns run.id

    try:
        export_result = await export_tenant_data(session, tenant_id)
        plaintext = export_result.to_json_bytes()
        compressed = gzip_json(plaintext)
        envelope = backup_crypto.encrypt_export(compressed, passphrase)
        # `plaintext`/`compressed`/`passphrase` are not referenced again.

        staging_dir = _staging_dir(tenant_id, effective_settings)
        staging_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = _artifact_path(tenant_id, run.id, effective_settings)
        artifact_path.write_bytes(envelope)

        run.artifact_path = str(artifact_path)
        run.artifact_size_bytes = len(envelope)
        run.artifact_sha256 = hashlib.sha256(envelope).hexdigest()
        run.table_counts = export_result.table_counts()
        run.status = "SUCCESS"
        run.completed_at = datetime.now(UTC)

        if config is not None and config.enabled:
            _push_to_destination(run, config, artifact_path)
        else:
            run.remote_push_status = "not_applicable"

    except Exception as exc:  # record failure on the run, don't raise
        # Callers get a FAILED run back (with `.error` set) rather than a
        # bubbled exception — the API layer maps run.status to the HTTP
        # response. This keeps "the export blew up partway through" and
        # "the export cleanly finished" symmetric: both are a run record,
        # never a stack trace surfaced to the caller.
        run.status = "FAILED"
        run.error = str(exc)
        run.completed_at = datetime.now(UTC)
        _log.exception("scheduled backup export failed for tenant %s", tenant_id)
    finally:
        await session.flush()

    if config is not None and run.status == "SUCCESS":
        await apply_retention(session, tenant_id, config, effective_settings)

    return run


def _push_to_destination(
    run: ScheduledBackupRun, config: ScheduledBackupConfig, artifact_path: Path
) -> None:
    destination = get_destination(config.destination_type)
    try:
        result = destination.push(artifact_path, config.destination_params)
        run.remote_push_status = result.status
    except RemotePushNotImplementedError as exc:
        run.remote_push_status = "stubbed_not_implemented"
        run.error = str(exc)
    except (DestinationConfigError, OSError) as exc:
        # The LOCAL export still succeeded (run.status stays SUCCESS) —
        # only the push failed. The artifact remains downloadable.
        run.remote_push_status = "failed"
        run.error = f"destination push failed: {exc}"
        _log.warning("backup destination push failed: %s", exc)


# --------------------------------------------------------------------- #
# Retention                                                               #
# --------------------------------------------------------------------- #


async def apply_retention(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    config: ScheduledBackupConfig,
    settings: Settings | None = None,
) -> int:
    """Delete runs (row + artifact file) beyond the config's retention
    policy. Returns the number of runs purged. Both knobs are optional
    and independent — a run is purged if it falls outside EITHER
    configured bound (keep_n OR keep_days), matching "keep the last N
    OR the last D days, whichever is more restrictive" — the common
    expectation for a retention policy with both knobs set.

    ``settings`` is accepted (unused today) for call-site symmetry with
    ``trigger_export`` and in case a future destination-aware retention
    policy needs it — artifact paths are already absolute (stored on
    the run row), so no settings lookup is needed to delete them.
    """
    del settings  # not needed today — see docstring
    if config.retention_keep_n is None and config.retention_keep_days is None:
        return 0

    result = await session.execute(
        select(ScheduledBackupRun)
        .where(
            ScheduledBackupRun.tenant_id == tenant_id,
            ScheduledBackupRun.status == "SUCCESS",
        )
        .order_by(ScheduledBackupRun.created_at.desc())
    )
    runs = list(result.scalars().all())

    to_purge: list[ScheduledBackupRun] = []
    if config.retention_keep_n is not None and len(runs) > config.retention_keep_n:
        to_purge.extend(runs[config.retention_keep_n :])
    if config.retention_keep_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=config.retention_keep_days)
        for r in runs:
            if r.created_at is not None and r.created_at < cutoff and r not in to_purge:
                to_purge.append(r)

    purged = 0
    for r in to_purge:
        if r.artifact_path:
            try:
                Path(r.artifact_path).unlink(missing_ok=True)
            except OSError:
                _log.warning("retention: could not delete artifact %s", r.artifact_path)
        await session.execute(
            delete(ScheduledBackupRun).where(ScheduledBackupRun.id == r.id)
        )
        purged += 1
    if purged:
        await session.flush()
    return purged


# --------------------------------------------------------------------- #
# Download                                                                #
# --------------------------------------------------------------------- #


def read_artifact_bytes(run: ScheduledBackupRun) -> bytes:
    """Return the raw ciphertext envelope bytes for a SUCCESS run.

    Callers (the API layer) stream this back verbatim — SAE Books does
    NOT decrypt it (see module docstring: the passphrase is gone).
    """
    if run.status != "SUCCESS" or not run.artifact_path:
        raise BackupRunNotFoundError("run has no downloadable artifact")
    return Path(run.artifact_path).read_bytes()
