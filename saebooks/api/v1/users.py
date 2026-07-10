"""Pure JSON users router — ``/api/v1/users`` + ``/api/v1/users/{id}/permissions``.

Phase 1 tier-2 entity. Follows the same pattern as accounts/tax_codes:

* Bearer-token auth via ``SAEBOOKS_DEV_API_TOKEN``.
* Admin-only gates on create/delete and the permissions PUT.
* Optimistic locking via ``If-Match: <version>`` on update/delete.
* Every write appends a row to ``change_log``.
* Password fields are NEVER exposed in any response model.
* Jinja ``/admin/users`` routes remain untouched — same service layer.

Admin check:
  Phase 1 uses the ``X-Admin: true`` request header as a lightweight
  privilege gate. The ``require_admin`` dependency returns 403 if the
  header is absent or not "true". Bearer auth is still required for
  ALL endpoints.

Tenant scoping:
  All handlers share a single ``Depends(get_session)`` session per
  request; ``app.current_tenant`` is set from the JWT before any query
  runs so FORCE-RLS gates every DB operation.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.api.v1.auth import require_bearer, resolve_tenant_id
from saebooks.api.v1.deps import get_session
from saebooks.api.v1.hard_delete_gate import hard_delete_admin_gate
from saebooks.api.v1.schemas import (
    PermissionOut,
    UserConflictBody,
    UserCreate,
    UserListOut,
    UserOut,
    UserPermissionOut,
    UserPermissionsBody,
    UserUpdate,
)
from saebooks.models.permission import Permission, UserPermission
from saebooks.models.user import VALID_ROLES, User, UserRole, has_at_least
from saebooks.services import permissions as perm_svc
from saebooks.services import users as svc
from saebooks.services.authz import current_user, require_permission_or_role
from saebooks.services.features import FLAG_THEMES, require_feature_inline
from saebooks.services.hard_delete import hard_delete_with_audit
from saebooks.services.theme import DEFAULT_THEME_ID

router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _require_admin(
    request: Request,
    x_admin: str | None = Header(default=None, alias="X-Admin"),
) -> None:
    """FastAPI dependency: enforce admin role.

    Order of checks (matches ``hard_delete_admin_gate``):

    1. If ``request.state.user`` is set (JWT bearer with a ``sub`` that
       resolves to a live User), require role ≥ ADMIN. ``X-Admin: true``
       is IGNORED on this path — a bookkeeper JWT cannot bypass with the
       header.
    2. Otherwise (static dev token, no user identity): fall back to the
       ``X-Admin: true`` header. This preserves the dev/test convenience
       where scripts hit ``/api/*`` with the static bearer + X-Admin.
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        role = getattr(request.state, "role", None) or user.role
        if not has_at_least(role, UserRole.ADMIN.value):
            raise HTTPException(403, "Admin role required")
        return
    if x_admin is None or x_admin.lower() != "true":
        raise HTTPException(403, "Admin privileges required")


async def _admin_fallback(request: Request) -> User | None:
    """Adapter: ``_require_admin``'s exact check, shaped for
    ``require_permission_or_role``'s ``fallback`` signature.

    ``_require_admin`` is a plain FastAPI dependency (extra
    ``Header``-injected param, returns ``None``) — this calls it
    directly with the header read straight off ``request.headers``
    (what FastAPI's ``Header(...)`` injection does under the hood
    anyway) so the below-tier path is BYTE-IDENTICAL to the coarse
    admin gate every one of these routes carried before this module.
    """
    await _require_admin(request, x_admin=request.headers.get("X-Admin"))
    return current_user(request)


def _parse_if_match(header: str | None) -> int | None:
    if header is None:
        return None
    cleaned = header.strip().strip('"').strip("W/").strip('"')
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(400, f"If-Match must be an integer version, got '{header}'") from exc


def _dump(user: User) -> dict[str, Any]:
    return json.loads(UserOut.model_validate(user).model_dump_json())


def _gate_non_default_theme(request: Request, preferred_theme: str | None) -> None:
    """404 (not 403) a request that sets a non-default theme below Offline.

    ``preferred_theme`` allow-list membership is validated separately
    (schema + ``services/users.py`` write-time check) — this only
    enforces the TIER boundary: CHARTER §12.1 "All themes (MYOB
    Classic, SS, TT, etc.)" is Offline+, but the single ``"default"``
    stock theme (and clearing the override with ``None``/``""``) must
    keep working at every tier, including Community, so a lower-tier
    install is never left broken by this gate. Mirrors the
    multi_currency pattern of gating only the specific request that
    crosses the tier boundary rather than the whole route (see
    ``require_feature_inline``'s docstring).
    """
    if preferred_theme and preferred_theme != DEFAULT_THEME_ID:
        require_feature_inline(FLAG_THEMES, request)


async def _gate_and_validate_role_id(
    session: AsyncSession, request: Request, tenant_id: UUID, role_id: UUID | None
) -> None:
    """404 below FLAG_GRANULAR_PERMISSIONS; 422 for a foreign/unknown role.

    Same conditional-gate shape as ``_gate_non_default_theme`` — NULL
    always works at every tier (falls back to the legacy ``role``
    string), only an explicit assignment crosses the tier boundary.
    The tenant-ownership check is defence-in-depth on top of the FK
    (a role id from another tenant would 422 here rather than silently
    succeeding and then failing to resolve anything at permission-
    check time).
    """
    if role_id is None:
        return
    from saebooks.services.features import FLAG_GRANULAR_PERMISSIONS

    require_feature_inline(FLAG_GRANULAR_PERMISSIONS, request)
    from saebooks.models.role import Role

    role = await session.get(Role, role_id)
    if role is None or role.tenant_id != tenant_id:
        raise HTTPException(422, f"Unknown role_id {role_id!r} for this tenant")


# ---------------------------------------------------------------------------
# List users (admin only)
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=UserListOut,
    dependencies=[
        Depends(require_permission_or_role("user.admin", _admin_fallback))
    ],
)
async def list_users(
    request: Request,
    role: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> UserListOut:
    tenant_id = resolve_tenant_id(request)
    items, total = await svc.list_active(
        session, limit=limit, offset=offset, role=role, tenant_id=tenant_id
    )
    return UserListOut(
        items=[UserOut.model_validate(u) for u in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Get one user (admin or self)
# The self-check is intentionally relaxed in Phase 1 — any valid bearer
# may read any user.  Phase 2 will tighten with JWT sub claim.
# ---------------------------------------------------------------------------


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    request: Request,
    user_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    tenant_id = resolve_tenant_id(request)
    user = await svc.get(session, user_id, tenant_id=tenant_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return UserOut.model_validate(user)


# ---------------------------------------------------------------------------
# Create user (admin only)
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=UserOut,
    status_code=201,
    dependencies=[
        Depends(require_permission_or_role("user.admin", _admin_fallback))
    ],
)
async def create_user(
    request: Request,
    payload: UserCreate,
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    if payload.role not in VALID_ROLES:
        raise HTTPException(422, f"Invalid role '{payload.role}'")

    # Themes (Wave B / FLAG_THEMES): allow-list membership already ran in
    # the pydantic schema; this is the TIER check — a non-default theme
    # is Offline+, "default" always works.
    _gate_non_default_theme(request, payload.preferred_theme)

    tenant_id = resolve_tenant_id(request)
    await _gate_and_validate_role_id(session, request, tenant_id, payload.role_id)
    # Reject duplicate username
    existing = await svc.get_by_username(session, payload.username)
    if existing is not None:
        raise HTTPException(409, "Username already exists")

    try:
        user = await svc.create_for_api(
            session,
            username=payload.username,
            display_name=payload.display_name,
            email=payload.email,
            role=payload.role,
            role_id=payload.role_id,
            preferred_theme=payload.preferred_theme,
            actor=f"api:{bearer[:8]}…",
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await session.refresh(user)
    body = _dump(user)
    return JSONResponse(body, status_code=201)


# ---------------------------------------------------------------------------
# Update user (PATCH with If-Match — admin or self profile fields)
# ---------------------------------------------------------------------------


@router.patch(
    "/{user_id}",
    responses={
        200: {"model": UserOut},
        409: {"model": UserConflictBody, "description": "Version mismatch"},
    },
)
async def update_user(
    request: Request,
    user_id: UUID,
    payload: UserUpdate,
    if_match: str | None = Header(default=None, alias="If-Match"),
    x_admin: str | None = Header(default=None, alias="X-Admin"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
) -> Any:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with user version is required")

    tenant_id = resolve_tenant_id(request)
    # Role check: prefer server-side role from request.state (set by
    # require_bearer when the JWT carries a sub). Falls back to the
    # X-Admin header for the static-bearer path used by tests/scripts.
    state_user = getattr(request.state, "user", None)
    if state_user is not None:
        state_role = getattr(request.state, "role", None) or state_user.role
        is_admin = has_at_least(state_role, UserRole.ADMIN.value)
    else:
        is_admin = x_admin is not None and x_admin.lower() == "true"
    updates = payload.model_dump(exclude_unset=True)

    # Non-admin may only update non-privileged fields (not role/role_id)
    if not is_admin and ("role" in updates or "role_id" in updates):
        raise HTTPException(403, "Admin privileges required to change role")

    # Validate role value if being changed
    if "role" in updates and updates["role"] not in VALID_ROLES:
        raise HTTPException(422, f"Invalid role '{updates['role']}'")

    # Themes (Wave B / FLAG_THEMES): only fires when preferred_theme is
    # actually being SET to a non-default value — see
    # _gate_non_default_theme's docstring.
    if "preferred_theme" in updates:
        _gate_non_default_theme(request, updates["preferred_theme"])

    # Granular permissions (D2): only fires when role_id is actually
    # being SET — see _gate_and_validate_role_id's docstring.
    if "role_id" in updates:
        await _gate_and_validate_role_id(
            session, request, tenant_id, updates["role_id"]
        )

    # Verify user belongs to this tenant before updating
    existing = await svc.get(session, user_id, tenant_id=tenant_id)
    if existing is None:
        raise HTTPException(404, "User not found")

    try:
        user = await svc.update_with_version(
            session,
            user_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
            **updates,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = UserConflictBody(
            detail="version mismatch",
            current=UserOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(404, msg) from exc
        raise HTTPException(422, msg) from exc

    await session.refresh(user)
    body = _dump(user)
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Delete (soft-deactivate) — admin only
# ---------------------------------------------------------------------------


@router.delete(
    "/{user_id}",
    responses={
        204: {"description": "Archived"},
        409: {"model": UserConflictBody, "description": "Version mismatch"},
    },
    dependencies=[
        Depends(require_permission_or_role("user.admin", _admin_fallback))
    ],
)
async def archive_user(
    request: Request,
    user_id: UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    bearer: str = Depends(require_bearer),
    session: AsyncSession = Depends(get_session),
    hard: bool = Depends(hard_delete_admin_gate),
) -> Any:
    tenant_id = resolve_tenant_id(request)
    existing = await svc.get(session, user_id, tenant_id=tenant_id)
    if existing is None:
        raise HTTPException(404, "User not found")

    if hard:
        await hard_delete_with_audit(
            session, existing, "users", getattr(request.state, "user", None)
        )
        await session.commit()
        return Response(status_code=204)

    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, "If-Match header with user version is required")

    try:
        user = await svc.archive_with_version(
            session,
            user_id,
            actor=f"api:{bearer[:8]}…",
            expected_version=expected,
        )
    except svc.VersionConflict as exc:
        await session.refresh(exc.current)
        body = UserConflictBody(
            detail="version mismatch",
            current=UserOut.model_validate(exc.current),
        ).model_dump(mode="json")
        return JSONResponse(body, status_code=409)
    if user is None:
        raise HTTPException(404, "User not found")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Permission catalogue — GET /api/v1/permissions (separate prefix)
# ---------------------------------------------------------------------------

permissions_router = APIRouter(
    prefix="/permissions",
    tags=["permissions"],
    dependencies=[Depends(require_bearer)],
)


@permissions_router.get("", response_model=list[PermissionOut])
async def list_permissions(
    session: AsyncSession = Depends(get_session),
) -> list[PermissionOut]:
    """Return the full permission catalogue (code + description)."""
    rows = (
        await session.execute(
            select(Permission).order_by(Permission.code)
        )
    ).scalars().all()
    return [PermissionOut.model_validate(p) for p in rows]


# ---------------------------------------------------------------------------
# Per-user permission endpoints — nested under /api/v1/users/{id}/permissions
# ---------------------------------------------------------------------------


@router.get("/{user_id}/permissions", response_model=list[UserPermissionOut])
async def get_user_permissions(
    request: Request,
    user_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> list[UserPermissionOut]:
    """Return the resolved permission set for a user.

    Each entry shows whether the permission is currently granted (True)
    or not (False), accounting for role grants and per-user overrides.
    """
    tenant_id = resolve_tenant_id(request)
    user = await svc.get(session, user_id, tenant_id=tenant_id)
    if user is None:
        raise HTTPException(404, "User not found")

    # Full catalogue
    catalogue = dict(
        (await session.execute(
            select(Permission.code, Permission.description)
        )).all()
    )

    # Resolved set (role grants U user grants minus user revokes)
    resolved = await perm_svc.resolve_permissions(session, user)

    return [
        UserPermissionOut(
            code=code,
            description=desc,
            resolved=(code in resolved),
        )
        for code, desc in sorted(catalogue.items())
    ]


@router.put(
    "/{user_id}/permissions",
    status_code=204,
    response_class=Response,
    # D4: permission.manage split from user.admin — granting/revoking
    # what a user/role can do is a higher-privilege action than
    # creating/deactivating a user. Below-tier fallback is unchanged
    # (still plain admin-only, same as every other route here).
    dependencies=[
        Depends(require_permission_or_role("permission.manage", _admin_fallback))
    ],
)
async def replace_user_permissions(
    request: Request,
    user_id: UUID,
    payload: UserPermissionsBody,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Replace the per-user permission overrides (admin only).

    ``grants`` and ``revokes`` replace the existing ``user_permissions``
    rows for this user. Any code not in either list falls back to the
    role-based grant.
    """
    tenant_id = resolve_tenant_id(request)
    user = await svc.get(session, user_id, tenant_id=tenant_id)
    if user is None:
        raise HTTPException(404, "User not found")

    # Validate all codes exist in catalogue
    all_codes_result = await session.execute(select(Permission.code))
    valid_codes = {row[0] for row in all_codes_result.all()}
    unknown = (set(payload.grants) | set(payload.revokes)) - valid_codes
    if unknown:
        raise HTTPException(422, f"Unknown permission codes: {sorted(unknown)}")

    # Overlap check — a code can't be in both grants and revokes
    overlap = set(payload.grants) & set(payload.revokes)
    if overlap:
        raise HTTPException(422, f"Codes cannot be in both grants and revokes: {sorted(overlap)}")

    # Clear existing per-user overrides, then insert new set
    await session.execute(
        delete(UserPermission).where(UserPermission.user_id == user_id)
    )
    for code in payload.grants:
        session.add(
            UserPermission(
                user_id=user_id,
                tenant_id=tenant_id,
                permission_code=code,
                granted=True,
            )
        )
    for code in payload.revokes:
        session.add(
            UserPermission(
                user_id=user_id,
                tenant_id=tenant_id,
                permission_code=code,
                granted=False,
            )
        )
    await session.commit()

    return Response(status_code=204)
