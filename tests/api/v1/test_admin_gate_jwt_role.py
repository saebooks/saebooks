"""Regression: JSON-API admin gates honour JWT role over X-Admin header.

Before this fix, ``users._require_admin`` and ``hard_delete_admin_gate``
treated ``X-Admin: true`` as the admin gate on the JSON API path. Any
caller with a bearer token (including a bookkeeper-role JWT) could
escalate by adding the header.

After fix: when the bearer is a JWT carrying a ``sub`` that resolves to
a live User, ``require_bearer`` stamps ``request.state.role`` and the
gates check that — ``X-Admin`` is ignored on the JWT path. The static
dev-bearer path (no JWT) still falls back to the X-Admin header so
existing scripts and tests keep working.
"""
from __future__ import annotations

import os
import uuid
import pytest

# SAEBOOKS_ENV is normally set by conftest at the project level, but be
# explicit here so this file is safe in isolation too.
os.environ.setdefault("SAEBOOKS_ENV", "test")

from httpx import AsyncClient
from sqlalchemy import delete as sa_delete

from saebooks.main import app
from saebooks.db import AsyncSessionLocal
from saebooks.models.user import User
from saebooks.services.jwt_tokens import _reset_secret_cache, create_access_token

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _mint(user: User) -> str:
    _reset_secret_cache()
    return create_access_token(
        {
            "sub": str(user.id),
            "role": user.role,
            "tenant_id": str(user.tenant_id),
        }
    )


@pytest.fixture
async def bookkeeper_user():
    user = User(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        username="gate-bookkeeper",
        email="gate-bookkeeper@test.invalid",
        role="bookkeeper",
    )
    async with AsyncSessionLocal() as session:
        session.add(user)
        await session.commit()
        await session.refresh(user)
    yield user
    async with AsyncSessionLocal() as session:
        await session.execute(sa_delete(User).where(User.id == user.id))
        await session.commit()


@pytest.fixture
async def admin_user():
    user = User(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        username="gate-admin",
        email="gate-admin@test.invalid",
        role="admin",
    )
    async with AsyncSessionLocal() as session:
        session.add(user)
        await session.commit()
        await session.refresh(user)
    yield user
    async with AsyncSessionLocal() as session:
        await session.execute(sa_delete(User).where(User.id == user.id))
        await session.commit()


async def test_bookkeeper_jwt_with_x_admin_header_is_rejected_on_users_list(
    bookkeeper_user: User,
) -> None:
    """A bookkeeper-role JWT cannot bypass /api/v1/users gate by adding X-Admin: true."""
    token = _mint(bookkeeper_user)
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.get(
            "/api/v1/users",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Admin": "true",
            },
        )
    assert r.status_code == 403, r.text


async def test_bookkeeper_jwt_with_x_admin_header_is_rejected_on_hard_delete(
    bookkeeper_user: User,
) -> None:
    """A bookkeeper-role JWT cannot hard-delete by adding X-Admin: true.

    Hits an arbitrary entity DELETE with ?hard=true and asserts 403
    before the gate even reaches the route handler.
    """
    token = _mint(bookkeeper_user)
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.delete(
            f"/api/v1/invoices/{uuid.uuid4()}?hard=true",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Admin": "true",
            },
        )
    assert r.status_code == 403, r.text


async def test_admin_jwt_passes_admin_gate(admin_user: User) -> None:
    """Sanity: an admin-role JWT passes /api/v1/users gate (no X-Admin needed)."""
    token = _mint(admin_user)
    async with AsyncClient(app=app, base_url="http://test") as client:
        r = await client.get(
            "/api/v1/users",
            headers={"Authorization": f"Bearer {token}"},
        )
    # 200 when admin; we don't care about the body — just that the gate
    # didn't 403.
    assert r.status_code == 200, r.text
