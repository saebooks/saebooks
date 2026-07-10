"""JSON router — ``/api/v1/roles`` (granular_permissions module, D2,
FLAG_GRANULAR_PERMISSIONS, Offline+).

Every route is:

* Feature-gated ``require_feature(FLAG_GRANULAR_PERMISSIONS)`` at the
  router level — 404 below Offline, same 404-not-403 convention as
  every other ``require_feature`` gate (a Community install can't
  enumerate a paid-tier capability's existence). CHARTER §6 places
  "granular permissions" in the Offline bundle.
* Admin-only (``_require_admin``, router level — same precedent as
  ``scheduled_backups.py``'s "least-privilege for a whole-tenant-
  authz-reach admin surface, tighter than the flag alone requires").
  Because ``_require_admin`` runs first, a non-admin on ANY tier gets
  403 before the tier check ever runs — matches the existing
  precedent's reasoning verbatim.
* Tenant-scoped via ``resolve_tenant_id`` + the standard
  ``Depends(get_session)`` RLS session; every service call also passes
  ``tenant_id`` explicitly (defence in depth).

The six starter roles (Owner/Admin/Bookkeeper/Approver/Read-only/
Payroll-only) always exist for a tenant by the time any of these
routes run (``services.roles.ensure_starter_roles`` self-heals on
every permission resolution — see ``services/authz.py``). Renaming a
starter role is allowed (D2 — "renameable"); deleting one is refused
(``services.roles.SystemRoleProtected`` — see that module's
docstring for the self-lockout reasoning). Only genuinely custom
roles (``is_system=False``) can be deleted.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.users import _require_admin
from saebooks.models.permission import Permission
from saebooks.services import roles as svc
from saebooks.services.features import FLAG_GRANULAR_PERMISSIONS, require_feature

router = APIRouter(
    prefix="/roles",
    tags=["roles"],
    dependencies=[
        Depends(require_bearer),
        Depends(_require_admin),
        Depends(require_feature(FLAG_GRANULAR_PERMISSIONS)),
    ],
)


# --------------------------------------------------------------------- #
# Schemas                                                                #
# --------------------------------------------------------------------- #


class RoleOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    base_role: str | None
    is_system: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RoleWithGrantsOut(RoleOut):
    grants: list[str]


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class RoleRename(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class RoleGrantsBody(BaseModel):
    codes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- #
# List / detail                                                          #
# --------------------------------------------------------------------- #


@router.get("", response_model=list[RoleOut])
async def list_roles(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[RoleOut]:
    tenant_id = resolve_tenant_id(request)
    await svc.ensure_starter_roles(session, tenant_id)
    items = await svc.list_roles(session, tenant_id)
    return [RoleOut.model_validate(r) for r in items]


@router.get("/{role_id}", response_model=RoleWithGrantsOut)
async def get_role(
    request: Request,
    role_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RoleWithGrantsOut:
    tenant_id = resolve_tenant_id(request)
    role = await svc.get_role(session, tenant_id, role_id)
    if role is None:
        raise HTTPException(404, "Role not found")

    from saebooks.services.permissions import role_grants

    grants = sorted(await role_grants(session, role_id))
    # NOTE: validate against RoleOut (the base schema, no ``grants``
    # field) first — validating the bare ORM ``role`` object directly
    # against RoleWithGrantsOut fails, because ``RoleWithGrantsOut``
    # requires ``grants: list[str]`` and the ORM object has no such
    # attribute (pydantic v2's from_attributes lookup raises "Field
    # required" — caught by the docker test suite, not by ruff/import
    # checks, since it's a runtime validation error on real data).
    base = RoleOut.model_validate(role).model_dump()
    return RoleWithGrantsOut(**base, grants=grants)


# --------------------------------------------------------------------- #
# Create / rename / delete                                               #
# --------------------------------------------------------------------- #


@router.post("", response_model=RoleOut, status_code=201)
async def create_role(
    request: Request,
    payload: RoleCreate,
    session: AsyncSession = Depends(get_session),
) -> RoleOut:
    tenant_id = resolve_tenant_id(request)
    try:
        role = await svc.create_role(session, tenant_id, payload.name)
    except svc.DuplicateRoleName as exc:
        raise HTTPException(409, str(exc)) from exc
    return RoleOut.model_validate(role)


@router.patch("/{role_id}", response_model=RoleOut)
async def rename_role(
    request: Request,
    role_id: uuid.UUID,
    payload: RoleRename,
    session: AsyncSession = Depends(get_session),
) -> RoleOut:
    tenant_id = resolve_tenant_id(request)
    try:
        role = await svc.rename_role(session, tenant_id, role_id, payload.name)
    except svc.DuplicateRoleName as exc:
        raise HTTPException(409, str(exc)) from exc
    except svc.RoleError as exc:
        raise HTTPException(404, str(exc)) from exc
    return RoleOut.model_validate(role)


@router.delete("/{role_id}", status_code=204)
async def delete_role(
    request: Request,
    role_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> None:
    tenant_id = resolve_tenant_id(request)
    try:
        await svc.delete_role(session, tenant_id, role_id)
    except svc.SystemRoleProtected as exc:
        raise HTTPException(409, str(exc)) from exc
    except svc.RoleError as exc:
        raise HTTPException(404, str(exc)) from exc


# --------------------------------------------------------------------- #
# Grants                                                                  #
# --------------------------------------------------------------------- #


@router.put("/{role_id}/grants", status_code=204)
async def set_role_grants(
    request: Request,
    role_id: uuid.UUID,
    payload: RoleGrantsBody,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Replace a role's grant set. Works for starter roles too — D2
    treats the starter grid as an editable default, not a floor."""
    tenant_id = resolve_tenant_id(request)
    role = await svc.get_role(session, tenant_id, role_id)
    if role is None:
        raise HTTPException(404, "Role not found")

    from sqlalchemy import select

    valid_codes = set(
        (await session.execute(select(Permission.code))).scalars().all()
    )
    unknown = set(payload.codes) - valid_codes
    if unknown:
        raise HTTPException(422, f"Unknown permission codes: {sorted(unknown)}")

    await svc.set_role_grants(session, tenant_id, role_id, payload.codes)
