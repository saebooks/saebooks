"""Tests for User model + role hierarchy + ForwardAuthMiddleware."""
from __future__ import annotations

import uuid
from datetime import UTC

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.user import User, UserRole, has_at_least, role_rank

# ----- pure role hierarchy (no DB, no HTTP) ---------------------------


def test_role_rank_ordering() -> None:
    assert role_rank("admin") > role_rank("accountant")
    assert role_rank("accountant") > role_rank("bookkeeper")
    assert role_rank("bookkeeper") > role_rank("readonly")
    assert role_rank("readonly") > role_rank("client")


def test_role_rank_unknown_is_negative() -> None:
    assert role_rank("root") == -1
    assert role_rank("") == -1


def test_has_at_least_admin_covers_everything() -> None:
    for role in UserRole:
        assert has_at_least(UserRole.ADMIN.value, role.value)


def test_has_at_least_client_covers_only_self() -> None:
    assert has_at_least(UserRole.CLIENT.value, UserRole.CLIENT.value)
    assert not has_at_least(UserRole.CLIENT.value, UserRole.READONLY.value)
    assert not has_at_least(UserRole.CLIENT.value, UserRole.ADMIN.value)


def test_has_at_least_unknown_fails_closed() -> None:
    assert not has_at_least("bogus", UserRole.READONLY.value)
    assert not has_at_least(UserRole.ADMIN.value, "bogus")


# ----- middleware auto-upsert on Remote-User header -------------------


async def _cleanup_user(username: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(User).where(User.username == username))
        await session.commit()


@pytest.fixture
async def unique_username() -> str:
    name = f"test-{uuid.uuid4().hex[:8]}"
    try:
        yield name
    finally:
        await _cleanup_user(name)


async def test_middleware_upserts_user_on_first_request(
    client: AsyncClient, unique_username: str
) -> None:
    r = await client.get(
        "/admin/whoami",
        headers={
            "Remote-User": unique_username,
            "Remote-Email": "new@example.com",
            "Remote-Name": "New User",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == unique_username
    assert body["email"] == "new@example.com"
    assert body["role"] == "readonly"  # default

    async with AsyncSessionLocal() as session:
        u = (
            await session.execute(
                select(User).where(User.username == unique_username)
            )
        ).scalar_one()
        assert u.role == "readonly"
        assert u.email == "new@example.com"
        assert u.display_name == "New User"
        assert u.last_seen_at is not None


async def test_middleware_updates_last_seen_on_subsequent_request(
    client: AsyncClient, unique_username: str
) -> None:
    # First hit — create
    await client.get(
        "/admin/whoami", headers={"Remote-User": unique_username}
    )
    async with AsyncSessionLocal() as session:
        u1 = (
            await session.execute(
                select(User).where(User.username == unique_username)
            )
        ).scalar_one()
        first_seen = u1.last_seen_at

    # Second hit — update last_seen
    await client.get(
        "/admin/whoami", headers={"Remote-User": unique_username}
    )
    async with AsyncSessionLocal() as session:
        u2 = (
            await session.execute(
                select(User).where(User.username == unique_username)
            )
        ).scalar_one()
    assert u2.last_seen_at is not None
    assert first_seen is not None
    assert u2.last_seen_at >= first_seen


async def test_whoami_401_without_remote_user_header(client: AsyncClient) -> None:
    r = await client.get("/admin/whoami")
    assert r.status_code == 401


async def test_archived_user_is_gated_off(
    client: AsyncClient, unique_username: str
) -> None:
    # Pre-create as archived
    async with AsyncSessionLocal() as session:
        from datetime import datetime
        u = User(
            username=unique_username,
            role="admin",
            archived_at=datetime.now(UTC),
        )
        session.add(u)
        await session.commit()

    r = await client.get(
        "/admin/whoami", headers={"Remote-User": unique_username}
    )
    # Even though they're admin-ranked in the DB, archived users don't
    # get request.state.user populated — require_user 401s.
    assert r.status_code == 401


# ----- role gate 403 vs 200 -------------------------------------------


async def test_admin_users_list_requires_admin(
    client: AsyncClient, unique_username: str
) -> None:
    """readonly user (default) gets 403 on /admin/users."""
    r = await client.get(
        "/admin/users", headers={"Remote-User": unique_username}
    )
    assert r.status_code == 403


async def test_admin_users_list_200_for_admin(
    client: AsyncClient, unique_username: str
) -> None:
    """Promoting the user to admin in the DB lets them through."""
    # First hit creates the user as readonly
    await client.get(
        "/admin/whoami", headers={"Remote-User": unique_username}
    )
    # Promote in-DB
    async with AsyncSessionLocal() as session:
        u = (
            await session.execute(
                select(User).where(User.username == unique_username)
            )
        ).scalar_one()
        u.role = "admin"
        await session.commit()

    r = await client.get(
        "/admin/users", headers={"Remote-User": unique_username}
    )
    assert r.status_code == 200
    assert unique_username in r.text  # their row shows up in the list


async def test_role_change_via_post(
    client: AsyncClient, unique_username: str
) -> None:
    """Admin changes another user's role via /admin/users/{id}/role."""
    # Create admin
    admin_name = f"admin-{uuid.uuid4().hex[:8]}"
    try:
        await client.get(
            "/admin/whoami", headers={"Remote-User": admin_name}
        )
        async with AsyncSessionLocal() as session:
            admin = (
                await session.execute(
                    select(User).where(User.username == admin_name)
                )
            ).scalar_one()
            admin.role = "admin"
            await session.commit()

        # Create target user
        await client.get(
            "/admin/whoami", headers={"Remote-User": unique_username}
        )
        async with AsyncSessionLocal() as session:
            target = (
                await session.execute(
                    select(User).where(User.username == unique_username)
                )
            ).scalar_one()
            target_id = target.id
        assert target.role == "readonly"

        # Admin POSTs a role change
        r = await client.post(
            f"/admin/users/{target_id}/role",
            data={"role": "accountant"},
            headers={"Remote-User": admin_name},
            follow_redirects=False,
        )
        assert r.status_code == 303

        async with AsyncSessionLocal() as session:
            target = await session.get(User, target_id)
            assert target is not None
            assert target.role == "accountant"
    finally:
        await _cleanup_user(admin_name)


async def test_bad_role_rejected(
    client: AsyncClient, unique_username: str
) -> None:
    """POSTing an unknown role bounces back with err=bad_role."""
    admin_name = f"admin-{uuid.uuid4().hex[:8]}"
    try:
        await client.get(
            "/admin/whoami", headers={"Remote-User": admin_name}
        )
        async with AsyncSessionLocal() as session:
            admin = (
                await session.execute(
                    select(User).where(User.username == admin_name)
                )
            ).scalar_one()
            admin.role = "admin"
            await session.commit()

        await client.get(
            "/admin/whoami", headers={"Remote-User": unique_username}
        )
        async with AsyncSessionLocal() as session:
            target = (
                await session.execute(
                    select(User).where(User.username == unique_username)
                )
            ).scalar_one()
            target_id = target.id
        r = await client.post(
            f"/admin/users/{target_id}/role",
            data={"role": "SUPERHERO"},
            headers={"Remote-User": admin_name},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "err=bad_role" in r.headers["location"]

        async with AsyncSessionLocal() as session:
            target = await session.get(User, target_id)
            assert target is not None
            assert target.role == "readonly"  # unchanged
    finally:
        await _cleanup_user(admin_name)
