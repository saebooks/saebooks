"""Permission resolution + starter-role grid (granular_permissions module).

Covers:
* ``resolve_permissions`` self-heals a tenant's starter roles/grants on
  first call (``ensure_starter_roles``) — a brand-new tenant with zero
  ``roles``/``role_permissions`` rows still resolves correctly.
* Each of the six starter roles gets exactly the D1-corrected grant
  set for a representative sample of codes (Bookkeeper never gets a
  post/void/lodge-class code; Approver gets all of them).
* ``users.role_id`` (explicit custom-role assignment) wins over the
  legacy ``role`` string when set.
* Archived users always resolve to the empty set regardless of role.
* User-level grant/revoke overrides compose correctly on top of the
  role grant set.

Uses the normal (BYPASSRLS owner-engine) test session — these are
pure resolution-logic tests, not RLS enforcement proofs. See
``tests/test_rls_roles.py`` / ``tests/test_rls_user_permissions.py``
for the Postgres-only RLS probes.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.permission import RolePermission, UserPermission
from saebooks.models.role import Role
from saebooks.models.tenant import Tenant
from saebooks.models.user import User
from saebooks.services import permissions as perm_svc

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def fresh_tenant():
    """A brand-new tenant with ZERO roles/role_permissions rows —
    proves the self-heal path, not just the migration-time backfill."""
    tid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(Tenant(id=tid, name=f"resolve-test-{tid}", slug=f"resolve-test-{tid}"))
        await session.commit()
    yield tid
    async with AsyncSessionLocal() as session:
        await session.execute(delete(User).where(User.tenant_id == tid))
        await session.execute(delete(UserPermission).where(UserPermission.tenant_id == tid))
        await session.execute(delete(RolePermission).where(RolePermission.tenant_id == tid))
        await session.execute(delete(Role).where(Role.tenant_id == tid))
        await session.execute(delete(Tenant).where(Tenant.id == tid))
        await session.commit()


async def _make_user(tenant_id: uuid.UUID, role: str, *, role_id=None) -> User:
    async with AsyncSessionLocal() as session:
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            username=f"u-{uuid.uuid4().hex[:10]}",
            role=role,
            role_id=role_id,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _resolve(user: User) -> frozenset[str]:
    async with AsyncSessionLocal() as session:
        # Re-fetch inside this session so lazy attributes are bound to
        # a live session (resolve_permissions issues its own queries).
        fresh = await session.get(User, user.id)
        return await perm_svc.resolve_permissions(session, fresh)


# ---------------------------------------------------------------------------
# Self-heal on a brand-new tenant
# ---------------------------------------------------------------------------


async def test_self_heals_starter_roles_for_a_brand_new_tenant(fresh_tenant) -> None:
    async with AsyncSessionLocal() as session:
        count = (
            await session.execute(
                select(Role).where(Role.tenant_id == fresh_tenant)
            )
        ).scalars().all()
        assert count == []  # nothing seeded yet — proves this isn't migration 0190's backfill

    owner = await _make_user(fresh_tenant, "owner")
    perms = await _resolve(owner)

    assert "dashboard.view" in perms
    assert "invoice.post" in perms  # Owner gets everything

    async with AsyncSessionLocal() as session:
        roles = (
            await session.execute(select(Role).where(Role.tenant_id == fresh_tenant))
        ).scalars().all()
        assert {r.name for r in roles} == {
            "Owner", "Admin", "Bookkeeper", "Approver", "Read-only", "Payroll-only",
        }


async def test_self_heal_is_idempotent(fresh_tenant) -> None:
    """Calling resolve_permissions twice never duplicates roles/grants."""
    owner = await _make_user(fresh_tenant, "owner")
    await _resolve(owner)
    await _resolve(owner)

    async with AsyncSessionLocal() as session:
        roles = (
            await session.execute(select(Role).where(Role.tenant_id == fresh_tenant))
        ).scalars().all()
        assert len(roles) == 6


# ---------------------------------------------------------------------------
# D1 — Bookkeeper is draft-only; Approver posts/voids/lodges
# ---------------------------------------------------------------------------

# (code, owner_gets_it, bookkeeper_gets_it, approver_gets_it, readonly_gets_it)
_D1_SAMPLE = [
    ("invoice.post", True, False, True, False),
    ("invoice.void", True, False, True, False),
    ("bill.post", True, False, True, False),
    ("payment.post", True, False, True, False),
    ("journal.post", True, False, True, False),
    ("bas.lodge", True, False, True, False),
    ("invoice.create", True, True, True, False),  # draft-only IS allowed
    ("invoice.view", True, True, True, True),
]


@pytest.mark.parametrize("code,o,b,a,r", _D1_SAMPLE)
async def test_d1_bookkeeper_draft_only_approver_posts(
    fresh_tenant, code, o, b, a, r
) -> None:
    owner = await _make_user(fresh_tenant, "owner")
    bookkeeper = await _make_user(fresh_tenant, "bookkeeper")
    accountant = await _make_user(fresh_tenant, "accountant")  # maps to Approver
    viewer = await _make_user(fresh_tenant, "viewer")

    assert (code in await _resolve(owner)) is o
    assert (code in await _resolve(bookkeeper)) is b
    assert (code in await _resolve(accountant)) is a
    assert (code in await _resolve(viewer)) is r


async def test_payroll_only_role_excludes_report_view(fresh_tenant) -> None:
    """Payroll-only has no legacy base_role — only reachable via role_id.
    Confirms it gets payroll-domain codes but NOT report.view (the
    draft's explicit design goal: payroll staff shouldn't see the P&L)."""
    owner = await _make_user(fresh_tenant, "owner")
    await _resolve(owner)  # trigger self-heal so the Payroll-only row exists

    async with AsyncSessionLocal() as session:
        payroll_role = (
            await session.execute(
                select(Role).where(
                    Role.tenant_id == fresh_tenant, Role.name == "Payroll-only"
                )
            )
        ).scalars().first()
    assert payroll_role is not None
    assert payroll_role.base_role is None

    payroll_user = await _make_user(fresh_tenant, "viewer", role_id=payroll_role.id)
    perms = await _resolve(payroll_user)
    assert "employee.view" in perms
    assert "time_entry.create" in perms
    assert "report.view" not in perms
    assert "invoice.post" not in perms


# ---------------------------------------------------------------------------
# role_id wins over the legacy role string
# ---------------------------------------------------------------------------


async def test_explicit_role_id_wins_over_legacy_role_string(fresh_tenant) -> None:
    owner = await _make_user(fresh_tenant, "owner")
    await _resolve(owner)  # self-heal

    async with AsyncSessionLocal() as session:
        approver_role = (
            await session.execute(
                select(Role).where(
                    Role.tenant_id == fresh_tenant, Role.name == "Approver"
                )
            )
        ).scalars().first()

    # users.role says "bookkeeper" (draft-only) but role_id points at
    # Approver — role_id must win.
    user = await _make_user(fresh_tenant, "bookkeeper", role_id=approver_role.id)
    perms = await _resolve(user)
    assert "invoice.post" in perms
    assert "bas.lodge" in perms


# ---------------------------------------------------------------------------
# Archived users, user overrides
# ---------------------------------------------------------------------------


async def test_archived_user_resolves_to_empty_set(fresh_tenant) -> None:
    from datetime import UTC, datetime

    async with AsyncSessionLocal() as session:
        user = User(
            id=uuid.uuid4(),
            tenant_id=fresh_tenant,
            username=f"archived-{uuid.uuid4().hex[:8]}",
            role="owner",
            archived_at=datetime.now(UTC),
        )
        session.add(user)
        await session.commit()

    perms = await _resolve(user)
    assert perms == frozenset()


async def test_user_grant_and_revoke_compose_over_role_grant(fresh_tenant) -> None:
    bookkeeper = await _make_user(fresh_tenant, "bookkeeper")
    baseline = await _resolve(bookkeeper)
    assert "invoice.post" not in baseline  # D1
    assert "invoice.create" in baseline

    async with AsyncSessionLocal() as session:
        await perm_svc.grant_user_permission(
            session,
            bookkeeper.id,
            "invoice.post",
            granted=True,
            tenant_id=fresh_tenant,
            granted_by="test",
        )
        await perm_svc.grant_user_permission(
            session,
            bookkeeper.id,
            "invoice.create",
            granted=False,
            tenant_id=fresh_tenant,
            granted_by="test",
        )

    updated = await _resolve(bookkeeper)
    assert "invoice.post" in updated  # per-user grant overrides role
    assert "invoice.create" not in updated  # per-user revoke overrides role
