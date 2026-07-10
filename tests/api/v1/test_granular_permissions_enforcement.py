"""HTTP-level enforcement tests for the granular_permissions module.

Covers:
* Below-tier (Community, default): a previously-ungated route
  (invoice.post) still works for the static-bearer caller — proves
  the ``no_additional_gate`` fix (require_user() would have 401'd
  this path; see authz.py's docstring).
* At-tier (Offline+): D1 enforcement — a Bookkeeper JWT gets 403
  posting/voiding; an Approver-mapped (``role="accountant"``) JWT is
  NOT blocked by the permission gate.
* No-lockout: an Owner/Admin JWT at-tier still reaches the route
  handler (404 "not found" for a bogus id, never 403) — the real risk
  flagged in review (a resolver/RLS bug would zero out permissions
  for EVERYONE including Owner).
* Gate boundary: ``/api/v1/roles`` 404s below Offline, 200s at Offline+.
* Custom-role create + per-tenant isolation via the roles API.

Mints real JWTs the same way ``test_admin_gate_jwt_role.py`` does —
``require_permission`` (the at-tier path) needs
``request.state.user`` populated, which only the JWT path provides.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest_asyncio

os.environ.setdefault("SAEBOOKS_ENV", "test")

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.user import User
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _mint(user: User) -> str:
    _reset_secret_cache()
    return create_access_token(
        {"sub": str(user.id), "role": user.role, "tenant_id": str(user.tenant_id)}
    )


async def _make_user(role: str, tenant_id: uuid.UUID = _TENANT) -> AsyncIterator[User]:
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        username=f"gp-{role}-{uuid.uuid4().hex[:8]}",
        email=f"gp-{role}-{uuid.uuid4().hex[:8]}@test.invalid",
        role=role,
    )
    async with AsyncSessionLocal() as session:
        session.add(user)
        await session.commit()
        await session.refresh(user)
    try:
        yield user
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_delete(User).where(User.id == user.id))
            await session.commit()


@pytest_asyncio.fixture
async def bookkeeper_user() -> AsyncIterator[User]:
    async for u in _make_user("bookkeeper"):
        yield u


@pytest_asyncio.fixture
async def approver_user() -> AsyncIterator[User]:
    # "accountant" is the legacy role string the Approver starter role
    # maps to via base_role — see models/role.py.
    async for u in _make_user("accountant"):
        yield u


@pytest_asyncio.fixture
async def admin_user() -> AsyncIterator[User]:
    async for u in _make_user("admin"):
        yield u


@pytest_asyncio.fixture
def offline_edition() -> AsyncIterator[None]:
    """Raise the process-wide edition to Offline for the test body.

    The autouse ``_restore_settings_edition`` fixture in conftest.py
    snapshots/restores ``settings.edition`` around every test, so this
    just needs to set it — no manual teardown required.
    """
    settings.edition = "offline"
    yield


# ---------------------------------------------------------------------------
# Below-tier: no_additional_gate preserves the static-token path
# ---------------------------------------------------------------------------


async def test_below_tier_static_bearer_can_still_reach_invoice_post_handler() -> None:
    """Community (default edition): the static dev bearer must NOT 401
    on a route this module wired (invoice.post carried only
    require_bearer before this module — see authz.no_additional_gate's
    docstring for why require_user() would have been a regression).

    Uses a bogus invoice id — a 401/403 here would mean the gate itself
    rejected the request; a 404 means it passed the gate and reached
    the "invoice not found" branch, which is what we're asserting.

    ``SAEBOOKS_DEV_API_TOKEN`` is resolved once at ``auth.py`` import
    time (env var if set, else an ephemeral value written BACK to
    ``os.environ`` — see ``auth._resolve_token``), so reading it here
    (after ``from saebooks.main import app`` has already imported that
    module) always returns the real live token.
    """
    token = os.environ["SAEBOOKS_DEV_API_TOKEN"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            f"/api/v1/invoices/{uuid.uuid4()}/post",
            headers={"Authorization": f"Bearer {token}", "If-Match": "1"},
        )
    # 404 "invoice not found" proves the gate passed through to the
    # handler; 401/403 would mean no_additional_gate regressed.
    assert r.status_code == 404, r.text


async def test_at_tier_static_bearer_can_still_reach_invoice_post_handler(
    offline_edition: None,
) -> None:
    """AT-TIER (Offline+): the static dev bearer must ALSO not 401.

    This is the mirror-image bug to the one above: ``require_bearer``
    never populates ``request.state.user`` for this token, so if
    ``require_permission_or_role`` took the fine-grained
    ``require_permission`` branch whenever the feature is on
    (regardless of whether a user is hydrated), this static/ops
    credential would 401 the instant a tenant enabled the feature —
    while the SAME request passed with the feature off (see the
    below-tier test above). That would make behaviour on this
    credential tier-DEPENDENT, which no other gate in this codebase
    does (``_require_admin`` et al. always carry the ``X-Admin``
    header carve-out for this exact token, at every tier). Caught in
    review; see ``authz.require_permission_or_role``'s docstring for
    the "AT-TIER static-bearer carve-out" fix this proves.
    """
    token = os.environ["SAEBOOKS_DEV_API_TOKEN"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            f"/api/v1/invoices/{uuid.uuid4()}/post",
            headers={"Authorization": f"Bearer {token}", "If-Match": "1"},
        )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# At-tier: D1 — Bookkeeper draft-only, Approver posts
# ---------------------------------------------------------------------------


async def test_at_tier_bookkeeper_403_on_invoice_post(
    offline_edition: None, bookkeeper_user: User
) -> None:
    token = _mint(bookkeeper_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            f"/api/v1/invoices/{uuid.uuid4()}/post",
            headers={"Authorization": f"Bearer {token}", "If-Match": "1"},
        )
    assert r.status_code == 403, r.text


async def test_at_tier_approver_not_blocked_by_permission_gate(
    offline_edition: None, approver_user: User
) -> None:
    """Approver (accountant) passes the permission gate — 404 "not
    found" for a bogus id, never 403 "forbidden"."""
    token = _mint(approver_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            f"/api/v1/invoices/{uuid.uuid4()}/post",
            headers={"Authorization": f"Bearer {token}", "If-Match": "1"},
        )
    assert r.status_code == 404, r.text


async def test_at_tier_bookkeeper_403_on_tax_code_manage(
    offline_edition: None, bookkeeper_user: User
) -> None:
    token = _mint(bookkeeper_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/tax_codes",
            headers={"Authorization": f"Bearer {token}"},
            json={"code": "TEST-D1", "name": "Test", "rate": "0.10"},
        )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# No-lockout — the real risk this module's review caught
# ---------------------------------------------------------------------------


async def test_at_tier_admin_not_locked_out_of_invoice_post(
    offline_edition: None, admin_user: User
) -> None:
    """An Admin JWT at-tier must still reach the handler — a
    resolver/RLS bug (e.g. the tenant-GUC gap fixed in authz.py) would
    zero out EVERYONE's permission set, including Admin's."""
    token = _mint(admin_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            f"/api/v1/invoices/{uuid.uuid4()}/post",
            headers={"Authorization": f"Bearer {token}", "If-Match": "1"},
        )
    assert r.status_code == 404, r.text
    assert r.status_code != 403, "admin locked out — permission resolver regression"


async def test_at_tier_admin_not_locked_out_of_users_list(
    offline_edition: None, admin_user: User
) -> None:
    token = _mint(admin_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Gate boundary — /api/v1/roles
# ---------------------------------------------------------------------------


async def test_roles_api_404_below_tier(admin_user: User) -> None:
    """Community (default edition, no offline_edition fixture): the
    whole /api/v1/roles router 404s, same convention as themes.py."""
    token = _mint(admin_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/roles", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404, r.text


async def test_roles_api_200_at_tier(offline_edition: None, admin_user: User) -> None:
    token = _mint(admin_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/roles", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    names = {row["name"] for row in r.json()}
    assert {"Owner", "Admin", "Bookkeeper", "Approver", "Read-only", "Payroll-only"} <= names


async def test_roles_api_requires_admin_even_at_tier(
    offline_edition: None, bookkeeper_user: User
) -> None:
    token = _mint(bookkeeper_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/roles", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Custom-role create + per-tenant isolation
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def other_tenant_admin() -> AsyncIterator[User]:
    """Admin user in a DIFFERENT tenant, for the isolation probe."""
    from sqlalchemy import delete as _sa_delete

    from saebooks.models.tenant import Tenant

    tid = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(Tenant(id=tid, name=f"gp-isolation-{tid}", slug=f"gp-isolation-{tid}"))
        await session.commit()

    user = User(
        id=uuid.uuid4(),
        tenant_id=tid,
        username=f"gp-other-admin-{uuid.uuid4().hex[:8]}",
        role="admin",
    )
    async with AsyncSessionLocal() as session:
        session.add(user)
        await session.commit()
        await session.refresh(user)
    try:
        yield user
    finally:
        from saebooks.models.permission import RolePermission
        from saebooks.models.role import Role

        async with AsyncSessionLocal() as session:
            await session.execute(_sa_delete(User).where(User.id == user.id))
            # ``GET /api/v1/roles`` (list_roles) self-heals the six
            # starter roles for this tenant on first call
            # (services.roles.ensure_starter_roles) -- the test hits
            # that endpoint for this fixture's tenant, so by teardown
            # time roles/role_permissions rows exist that FK-reference
            # `tid`. Deleting the tenant before these is a
            # ForeignKeyViolationError on roles_tenant_id_fkey (caught
            # by the docker suite, not by ruff/import checks -- an
            # "ERROR at teardown", not a test assertion failure; the
            # test body itself passed). Delete children first,
            # role_permissions before roles (its own FK).
            await session.execute(
                _sa_delete(RolePermission).where(RolePermission.tenant_id == tid)
            )
            await session.execute(_sa_delete(Role).where(Role.tenant_id == tid))
            await session.execute(_sa_delete(Tenant).where(Tenant.id == tid))
            await session.commit()


async def test_custom_role_created_by_one_tenant_invisible_to_another(
    offline_edition: None, admin_user: User, other_tenant_admin: User
) -> None:
    # NOTE: _mint() resets the process-wide ephemeral JWT signing key
    # on every call (jwt_tokens._reset_secret_cache — a testing hook
    # that, since SAEBOOKS_SECRET_KEY is unset in this suite, causes
    # `_secret_key()` to regenerate a BRAND NEW random secret rather
    # than re-read a stable one from settings). Minting token_a then
    # token_b sequentially would invalidate token_a's signature before
    # it's ever used -- every other test in this file only mints ONE
    # token, so this two-token case is the first to hit it. Reset once,
    # then mint both under the SAME key.
    _reset_secret_cache()
    token_a = create_access_token(
        {"sub": str(admin_user.id), "role": admin_user.role, "tenant_id": str(admin_user.tenant_id)}
    )
    token_b = create_access_token(
        {
            "sub": str(other_tenant_admin.id),
            "role": other_tenant_admin.role,
            "tenant_id": str(other_tenant_admin.tenant_id),
        }
    )
    role_name = f"HR Admin {uuid.uuid4().hex[:8]}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/roles",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"name": role_name},
        )
        assert create_resp.status_code == 201, create_resp.text
        created_id = create_resp.json()["id"]

        list_b = await client.get(
            "/api/v1/roles", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert list_b.status_code == 200, list_b.text
        assert created_id not in {row["id"] for row in list_b.json()}

        detail_b = await client.get(
            f"/api/v1/roles/{created_id}", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert detail_b.status_code == 404, detail_b.text

    # Cleanup
    async with AsyncSessionLocal() as session:
        from sqlalchemy import delete as _sa_delete

        from saebooks.models.role import Role

        await session.execute(_sa_delete(Role).where(Role.id == uuid.UUID(created_id)))
        await session.commit()


async def test_custom_role_starts_with_zero_grants(
    offline_edition: None, admin_user: User
) -> None:
    token = _mint(admin_user)
    role_name = f"Zero Grants {uuid.uuid4().hex[:8]}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/roles",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": role_name},
        )
        assert create_resp.status_code == 201, create_resp.text
        role_id = create_resp.json()["id"]

        detail = await client.get(
            f"/api/v1/roles/{role_id}", headers={"Authorization": f"Bearer {token}"}
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["grants"] == []

    async with AsyncSessionLocal() as session:
        from sqlalchemy import delete as _sa_delete

        from saebooks.models.role import Role

        await session.execute(_sa_delete(Role).where(Role.id == uuid.UUID(role_id)))
        await session.commit()


async def test_system_role_cannot_be_deleted(
    offline_edition: None, admin_user: User
) -> None:
    token = _mint(admin_user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        list_resp = await client.get(
            "/api/v1/roles", headers={"Authorization": f"Bearer {token}"}
        )
        owner_row = next(r for r in list_resp.json() if r["name"] == "Owner")

        del_resp = await client.delete(
            f"/api/v1/roles/{owner_row['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert del_resp.status_code == 409, del_resp.text
