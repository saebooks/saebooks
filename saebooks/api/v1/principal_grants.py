"""Tenant-side grant management — "who can act as my books".

Mounted at ``/api/v1/principal-grants``. This is the GRANTING side: a tenant
admin creates / revokes / lists the grants that let a principal (accountant /
bank) act as THEIR tenant.

Why this runs under the granting tenant's USER session
------------------------------------------------------
Every route here uses the ordinary user auth + session machinery
(``require_bearer`` -> ``get_session`` -> ``app.current_tenant`` bound from the
user's JWT). That means the FORCE-RLS ``tenant_isolation`` policy on
``principal_tenant_grants`` applies:

* INSERT/UPDATE are checked by the policy ``WITH CHECK`` clause —
  ``tenant_id`` MUST equal ``app.current_tenant``. So a tenant can ONLY create
  a grant binding a principal to ITSELF; it can never forge a grant into
  another tenant (the DB rejects it). This is the load-bearing control and is
  proven by ``test_tenant_cannot_forge_grant_for_foreign_tenant`` (service
  layer) + the API-level forge test.
* SELECT is filtered to the tenant's own grants, so a tenant admin only ever
  sees who can access THEIR books.

We deliberately do NOT use a SECURITY DEFINER write path or the owner role for
grant management — the whole point is that the database, not the application,
enforces "a tenant may only grant to itself".

Authorization
-------------
Grant management is an admin action (it changes who can read/write the
tenant's ledger). We reuse ``_require_admin`` (role >= ADMIN), the same gate
that protects user management.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_active_user_id, get_session
from saebooks.api.v1.users import _require_admin
from saebooks.models.user import VALID_ROLES

logger = logging.getLogger("saebooks.api.v1.principal_grants")

router = APIRouter(
    prefix="/principal-grants",
    tags=["principal-grants"],
    dependencies=[Depends(require_bearer)],
)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class GrantCreate(BaseModel):
    principal_id: uuid.UUID
    # The scoped role the principal will operate under inside this tenant.
    # Must be a known UserRole (the DB coherence trigger fails closed anyway).
    role: str


class GrantOut(BaseModel):
    id: uuid.UUID
    principal_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    status: str
    granted_at: datetime | None = None
    revoked_at: datetime | None = None


# --------------------------------------------------------------------------- #
# List — the tenant's own grants (RLS-filtered to app.current_tenant).
# --------------------------------------------------------------------------- #


@router.get(
    "", response_model=list[GrantOut], dependencies=[Depends(_require_admin)]
)
async def list_grants(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[GrantOut]:
    rows = (
        await session.execute(
            text(
                "SELECT id, principal_id, tenant_id, role, status, "
                "granted_at, revoked_at FROM principal_tenant_grants "
                "ORDER BY granted_at DESC"
            )
        )
    ).all()
    return [
        GrantOut(
            id=r.id,
            principal_id=r.principal_id,
            tenant_id=r.tenant_id,
            role=r.role,
            status=r.status,
            granted_at=r.granted_at,
            revoked_at=r.revoked_at,
        )
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Create — bind a principal to THIS tenant (RLS WITH CHECK enforces self).
# --------------------------------------------------------------------------- #


@router.post(
    "",
    response_model=GrantOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
async def create_grant(
    body: GrantCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor_user_id: uuid.UUID = Depends(get_active_user_id),
) -> GrantOut:
    """Create an active grant for the AUTHENTICATED tenant.

    The ``tenant_id`` is taken from the authenticated session
    (``resolve_tenant_id``), NOT from the request body — a tenant cannot ask
    to grant on behalf of another tenant. Even if the value were forged, the
    ``tenant_isolation`` WITH CHECK would reject an INSERT whose tenant_id !=
    app.current_tenant. The role is validated app-side (and the DB coherence
    trigger fails closed on an unknown role).
    """
    if body.role not in VALID_ROLES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"role must be one of {sorted(VALID_ROLES)}",
        )
    tenant_id = resolve_tenant_id(request)
    grant_id = uuid.uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO principal_tenant_grants "
                "(id, principal_id, tenant_id, role, status, "
                "granted_by_user_id) "
                "VALUES (:id, :pid, :tid, :role, 'active', :uid)"
            ),
            {
                "id": str(grant_id),
                "pid": str(body.principal_id),
                "tid": str(tenant_id),
                "role": body.role,
                "uid": str(actor_user_id),
            },
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        msg = str(exc).lower()
        # Duplicate active grant (partial unique index) -> 409.
        if "uq_principal_tenant_grant_active" in msg or "unique" in msg:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "an active grant already exists for this principal",
            ) from exc
        # FK violation (unknown principal) -> 400.
        if "foreign key" in msg or "violates foreign key" in msg:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "unknown principal_id"
            ) from exc
        logger.warning("create_grant failed: %s", exc)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "could not create grant"
        ) from exc

    return GrantOut(
        id=grant_id,
        principal_id=body.principal_id,
        tenant_id=tenant_id,
        role=body.role,
        status="active",
        granted_at=None,
        revoked_at=None,
    )


# --------------------------------------------------------------------------- #
# Revoke — immediate. RLS confines the UPDATE to the tenant's own grants.
# --------------------------------------------------------------------------- #


@router.delete(
    "/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_require_admin)],
)
async def revoke_grant(
    grant_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Revoke an active grant. Access is removed immediately for new act-as.

    The UPDATE runs under ``app.current_tenant`` = the caller's tenant, so the
    ``tenant_isolation`` policy means a tenant can only revoke its OWN grants —
    a grant id belonging to another tenant simply matches zero rows (404). We
    soft-delete (status='revoked') to keep the audit trail; the partial unique
    index is on active rows only, so a fresh grant can be re-issued later.
    """
    result = await session.execute(
        text(
            "UPDATE principal_tenant_grants "
            "SET status='revoked', revoked_at=now() "
            "WHERE id = :id AND status='active'"
        ),
        {"id": str(grant_id)},
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "active grant not found"
        )
