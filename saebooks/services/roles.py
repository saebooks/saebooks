"""Tenant-scoped role management (granular_permissions module, D2).

Two responsibilities:

1. ``ensure_starter_roles`` — idempotent self-heal. Migration
   ``0190_roles_table`` bulk-backfilled the six starter roles (+ their
   grants, via ``0194_role_permissions_rls``) for every tenant
   that existed at migration time. Any tenant created AFTER that
   (signup, ephemeral demo, principal-minted — see ``api/v1/signup.py``,
   ``services/ephemeral_demo.py``, ``services/principal.py``) needs the
   same seed, and this is the single defensive place it happens:
   called at the top of ``services.permissions.resolve_permissions`` on
   EVERY request, so no tenant-creation code path can ever forget it
   and lock its own users out (advisor review flagged this as the
   real risk in this module — a tenant whose roles/grants never got
   seeded would resolve to zero permissions for everyone, including
   its own Owner, the moment ``require_permission`` is wired to any
   route). Cheap on the hot path: one indexed SELECT to check
   "already seeded" before doing any write.

2. Custom-role CRUD (``list_roles`` / ``create_role`` / ``rename_role``
   / ``set_role_grants`` / ``delete_role``) — the FLAG_GRANULAR_
   PERMISSIONS-gated capability itself (D2 "real custom roles"). The
   tier gate + admin check live in ``api/v1/roles.py``; this layer
   trusts the caller and only enforces DATA invariants (tenant
   ownership, system-role delete protection, unique name per tenant).
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.permission import RolePermission
from saebooks.models.role import STARTER_ROLES, Role
from saebooks.services.starter_role_grants import codes_for_role_name


class RoleError(Exception):
    """Base for role-service errors the API layer maps to HTTP status."""


class DuplicateRoleName(RoleError):
    """A role with this name already exists for the tenant."""


class SystemRoleProtected(RoleError):
    """Refused to delete (or rename away from being findable) a
    system starter role — see ``delete_role``'s docstring."""


async def ensure_starter_roles(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Idempotently seed the six starter roles + their grants.

    Two-phase: create any missing ``Role`` row, THEN grant every
    missing role its full starter grant set. Both phases are
    independently idempotent (safe to call on a tenant that's
    half-seeded from a prior partial failure) — a role that already
    exists is never re-created; a grant that already exists is never
    duplicated (checked via NOT EXISTS per code, not a bulk upsert,
    since role_permissions' PK is (role_id, permission_code) and the
    grant set per role is small, ~20-134 rows).
    """
    existing_roles = (
        await session.execute(
            select(Role.id, Role.name).where(
                Role.tenant_id == tenant_id, Role.is_system.is_(True)
            )
        )
    ).all()
    existing_names = {name for _id, name in existing_roles}
    role_ids_by_name: dict[str, uuid.UUID] = {
        name: rid for rid, name in existing_roles
    }

    missing = [
        (name, base_role)
        for name, base_role in STARTER_ROLES
        if name not in existing_names
    ]
    for name, base_role in missing:
        role = Role(
            tenant_id=tenant_id, name=name, base_role=base_role, is_system=True
        )
        session.add(role)
        await session.flush()
        role_ids_by_name[name] = role.id

    if missing:
        await session.commit()

    # --- Phase 2: grant every starter role its full starter set. -----
    granted_any = False
    for name, role_id in role_ids_by_name.items():
        target = codes_for_role_name(name)
        if not target:
            continue
        existing_codes = set(
            (
                await session.execute(
                    select(RolePermission.permission_code).where(
                        RolePermission.role_id == role_id
                    )
                )
            )
            .scalars()
            .all()
        )
        to_add = target - existing_codes
        for code in to_add:
            session.add(
                RolePermission(
                    role_id=role_id, tenant_id=tenant_id, permission_code=code
                )
            )
            granted_any = True

    if granted_any:
        await session.commit()


async def list_roles(session: AsyncSession, tenant_id: uuid.UUID) -> list[Role]:
    result = await session.execute(
        select(Role).where(Role.tenant_id == tenant_id).order_by(Role.name)
    )
    return list(result.scalars().all())


async def get_role(
    session: AsyncSession, tenant_id: uuid.UUID, role_id: uuid.UUID
) -> Role | None:
    result = await session.execute(
        select(Role).where(Role.id == role_id, Role.tenant_id == tenant_id)
    )
    return result.scalars().first()


async def create_role(
    session: AsyncSession, tenant_id: uuid.UUID, name: str
) -> Role:
    """Create a genuinely custom role — starts with ZERO grants.

    An admin builds it up via ``set_role_grants``. Never has a
    ``base_role`` (only reachable via ``users.role_id`` — see
    ``models/role.py``).
    """
    role = Role(tenant_id=tenant_id, name=name.strip(), base_role=None, is_system=False)
    session.add(role)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateRoleName(
            f"A role named {name!r} already exists for this tenant"
        ) from exc
    await session.refresh(role)
    return role


async def rename_role(
    session: AsyncSession, tenant_id: uuid.UUID, role_id: uuid.UUID, new_name: str
) -> Role:
    """Rename ANY role, including a system starter (D2 — "renameable").

    Renaming never affects ``base_role`` (the legacy-string bridge) or
    the grant set — only the display name.
    """
    role = await get_role(session, tenant_id, role_id)
    if role is None:
        raise RoleError(f"Role {role_id} not found for this tenant")
    role.name = new_name.strip()
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateRoleName(
            f"A role named {new_name!r} already exists for this tenant"
        ) from exc
    await session.refresh(role)
    return role


async def set_role_grants(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
    codes: Iterable[str],
) -> None:
    """Replace the grant set for one tenant-owned role.

    Full delete-and-reinsert (same "safest for a full-set swap"
    reasoning the pre-D2 version of this function used) — scoped to
    ONE role_id at a time now instead of a bare global role string.
    """
    target = frozenset(codes)
    existing = frozenset(
        (
            await session.execute(
                select(RolePermission.permission_code).where(
                    RolePermission.role_id == role_id,
                    RolePermission.tenant_id == tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if existing == target:
        return
    await session.execute(
        delete(RolePermission).where(
            RolePermission.role_id == role_id,
            RolePermission.tenant_id == tenant_id,
        )
    )
    for code in sorted(target):
        session.add(
            RolePermission(role_id=role_id, tenant_id=tenant_id, permission_code=code)
        )
    await session.commit()


async def delete_role(
    session: AsyncSession, tenant_id: uuid.UUID, role_id: uuid.UUID
) -> None:
    """Delete a tenant-owned CUSTOM role. Refuses a system starter role.

    Deleting "Owner" (or any of the other five starters) out from
    under every user resolved onto it via the legacy ``base_role``
    bridge would silently strip permissions from every such user the
    next time ``resolve_permissions`` runs — a self-lockout footgun a
    UI click shouldn't be able to trigger. If a tenant genuinely wants
    to retire a starter role's NAME, ``rename_role`` covers that;
    deletion stays reserved for genuinely custom roles.
    """
    role = await get_role(session, tenant_id, role_id)
    if role is None:
        raise RoleError(f"Role {role_id} not found for this tenant")
    if role.is_system:
        raise SystemRoleProtected(
            f"Role {role.name!r} is a starter role and cannot be deleted "
            "(rename it instead)"
        )
    await session.delete(role)
    await session.commit()


__all__ = [
    "DuplicateRoleName",
    "RoleError",
    "SystemRoleProtected",
    "create_role",
    "delete_role",
    "ensure_starter_roles",
    "get_role",
    "list_roles",
    "rename_role",
    "set_role_grants",
]
