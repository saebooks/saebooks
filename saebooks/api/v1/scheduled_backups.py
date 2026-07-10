"""JSON router — ``/api/v1/scheduled-backups`` (planned-modules Wave E,
FLAG_SCHEDULED_BACKUPS, Pro+).

Every route is:

* Feature-gated ``require_feature(FLAG_SCHEDULED_BACKUPS)`` — 404 below
  Pro tier, matching the same ``dependencies=[Depends(require_feature(...))]``
  per-route pattern as ``/api/v1/admin/sql/execute`` (the closest
  precedent: also Pro+, also a whole-tenant-data-reach admin surface).
* Admin-only (``_require_admin``, router-level, same as ``admin.py``) —
  a per-tenant data export is deliberately least-privilege; this is
  Wave E's own choice (the spec only required the feature gate), noted
  here so it's an explicit decision, not an accident. Because
  ``_require_admin`` runs first at the router level, a non-admin user
  on ANY tier gets 403 before the tier check ever runs — the 404-not-
  403 guarantee therefore applies to "authenticated admin below Pro",
  exactly the scenario the guardrail is protecting (a lower-tier
  INSTALL never learns this feature exists via a distinctive error
  code). This mirrors ``admin.py``'s existing precedent verbatim, not a
  new ordering invented for this router.
* Tenant-scoped via the standard ``Depends(get_session)`` RLS session +
  an explicit ``tenant_id`` filter in every service call (defence in
  depth, same posture as every other v1 router).

The actual isolation guarantee — that an export can never contain
another tenant's rows — lives in ``services/backup_export.py``, not
here. This router is deliberately thin: parse request, resolve tenant,
call the service, shape the response.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.users import _require_admin
from saebooks.services import scheduled_backups as svc
from saebooks.services.backup_crypto import WeakPassphraseError
from saebooks.services.backup_destinations import DestinationConfigError
from saebooks.services.features import FLAG_SCHEDULED_BACKUPS, require_feature

router = APIRouter(
    prefix="/scheduled-backups",
    tags=["scheduled-backups"],
    dependencies=[Depends(require_bearer), Depends(_require_admin)],
)


def _get_optional_user_id(request: Request) -> uuid.UUID | None:
    """Real user id, or ``None`` — NOT ``deps.get_active_user_id``.

    ``created_by``/``updated_by``/``requested_by`` on the scheduled-backup
    tables are ``FOREIGN KEY REFERENCES users(id)`` (nullable). ``deps.
    get_active_user_id`` is purpose-built for ``audit_log.actor_user_id``,
    which is a plain NOT NULL uuid column with NO foreign key — it falls
    back to ``SYSTEM_ACTOR_USER_ID`` (the nil UUID) for the static
    dev-bearer path (tests/scripts), and that sentinel has no row in
    ``users``. Using it here would raise a FOREIGN KEY VIOLATION on every
    dev/test-bearer request. Mirrors the guard in ``api/v1/leave.py``'s
    ``adjust_balance`` — only stamp a real hydrated ``request.state.user``,
    else leave the column NULL.
    """
    user = getattr(request.state, "user", None)
    uid = getattr(user, "id", None)
    return uid if isinstance(uid, uuid.UUID) else None


# --------------------------------------------------------------------- #
# Schemas                                                                #
# --------------------------------------------------------------------- #


class ConfigIn(BaseModel):
    enabled: bool = True
    destination_type: str = "local_path"
    destination_params: dict[str, Any] = Field(default_factory=dict)
    retention_keep_n: int | None = Field(default=None, ge=1)
    retention_keep_days: int | None = Field(default=None, ge=1)
    # "client" is the only value implemented — see
    # services/scheduled_backups.py's ManagedBySaeNotImplementedError.
    managed_by: str = "client"


class ConfigOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    enabled: bool
    destination_type: str
    destination_params: dict[str, Any]
    retention_keep_n: int | None
    retention_keep_days: int | None
    managed_by: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ExportRequest(BaseModel):
    # The client's own passphrase — SAE Books never stores this (see
    # services/scheduled_backups.py module docstring). min_length here
    # is a fast-fail mirror of backup_crypto's own floor; the service
    # layer is the source of truth.
    passphrase: str = Field(min_length=12)


class RunOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    config_id: uuid.UUID | None
    status: str
    destination_type: str
    artifact_size_bytes: int | None
    artifact_sha256: str | None
    table_counts: dict[str, int] | None
    remote_push_status: str
    error: str | None
    requested_by: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RunListOut(BaseModel):
    items: list[RunOut]
    limit: int
    offset: int


# --------------------------------------------------------------------- #
# Config                                                                  #
# --------------------------------------------------------------------- #


@router.get(
    "/config",
    response_model=ConfigOut | None,
    dependencies=[Depends(require_feature(FLAG_SCHEDULED_BACKUPS))],
)
async def get_config(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ConfigOut | None:
    tenant_id = resolve_tenant_id(request)
    config = await svc.get_config(session, tenant_id)
    if config is None:
        return None
    return ConfigOut.model_validate(config)


@router.put(
    "/config",
    response_model=ConfigOut,
    dependencies=[Depends(require_feature(FLAG_SCHEDULED_BACKUPS))],
)
async def put_config(
    body: ConfigIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user_id: uuid.UUID | None = Depends(_get_optional_user_id),
) -> ConfigOut:
    tenant_id = resolve_tenant_id(request)
    try:
        config = await svc.upsert_config(
            session,
            tenant_id,
            enabled=body.enabled,
            destination_type=body.destination_type,
            destination_params=body.destination_params,
            retention_keep_n=body.retention_keep_n,
            retention_keep_days=body.retention_keep_days,
            managed_by=body.managed_by,
            user_id=user_id,
        )
    except svc.ManagedBySaeNotImplementedError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (DestinationConfigError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.commit()
    return ConfigOut.model_validate(config)


# --------------------------------------------------------------------- #
# Trigger export                                                         #
# --------------------------------------------------------------------- #


@router.post(
    "/export",
    response_model=RunOut,
    dependencies=[Depends(require_feature(FLAG_SCHEDULED_BACKUPS))],
)
async def post_export(
    body: ExportRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user_id: uuid.UUID | None = Depends(_get_optional_user_id),
) -> RunOut:
    tenant_id = resolve_tenant_id(request)
    try:
        run = await svc.trigger_export(
            session, tenant_id, passphrase=body.passphrase, user_id=user_id
        )
    except WeakPassphraseError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (DestinationConfigError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.commit()
    return RunOut.model_validate(run)


# --------------------------------------------------------------------- #
# Runs — list / detail / download                                        #
# --------------------------------------------------------------------- #


@router.get(
    "/runs",
    response_model=RunListOut,
    dependencies=[Depends(require_feature(FLAG_SCHEDULED_BACKUPS))],
)
async def list_runs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> RunListOut:
    tenant_id = resolve_tenant_id(request)
    runs = await svc.list_runs(session, tenant_id, limit=limit, offset=offset)
    return RunListOut(
        items=[RunOut.model_validate(r) for r in runs], limit=limit, offset=offset
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunOut,
    dependencies=[Depends(require_feature(FLAG_SCHEDULED_BACKUPS))],
)
async def get_run(
    run_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    tenant_id = resolve_tenant_id(request)
    run = await svc.get_run(session, tenant_id, run_id)
    if run is None:
        raise HTTPException(404, "Backup run not found")
    return RunOut.model_validate(run)


@router.get(
    "/runs/{run_id}/download",
    dependencies=[Depends(require_feature(FLAG_SCHEDULED_BACKUPS))],
)
async def download_run(
    run_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Stream back the ENCRYPTED artifact verbatim.

    SAE Books does not decrypt this — the passphrase that encrypted it
    was never stored (see services/scheduled_backups.py). The client
    decrypts locally with ``services/backup_crypto.decrypt_export``'s
    documented envelope format (SAEBKX01: magic + salt + nonce +
    AES-256-GCM ciphertext, scrypt-derived key) or their own compatible
    tooling.
    """
    tenant_id = resolve_tenant_id(request)
    run = await svc.get_run(session, tenant_id, run_id)
    if run is None:
        raise HTTPException(404, "Backup run not found")
    if run.status != "SUCCESS" or not run.artifact_path:
        raise HTTPException(409, f"Run is not downloadable (status={run.status})")
    data = svc.read_artifact_bytes(run)
    filename = f"saebooks-backup-{run.created_at:%Y%m%dT%H%M%SZ}.enc"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
