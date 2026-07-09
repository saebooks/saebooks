"""Local fixtures for grpc tests.

The api_tokens grpc tests reference `db_session`, `seeded_company`,
and `seeded_user` fixtures that do not exist anywhere else in the
suite. Provide minimal implementations here so the tests can run.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.user import User, UserRole

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession bound to the test DB."""
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def seeded_company() -> Company:
    """Return the seed company (oldest by created_at)."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company


@pytest_asyncio.fixture
async def seeded_user() -> User:
    """Return (creating if needed) a non-admin user in the default tenant.

    Also purges any api_tokens owned by this user before yielding so
    tests asserting on the token list (e.g.
    ``test_list_excludes_revoked_by_default``) start with a clean set
    rather than the accumulation from prior tests in the same session.
    """
    from sqlalchemy import text

    username = "grpc-test-user"
    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(
                select(User).where(User.username == username)
            )
        ).scalars().first()
        if user is None:
            user = User(
                tenant_id=_DEFAULT_TENANT_ID,
                username=username,
                role=UserRole.BOOKKEEPER.value,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        await session.execute(
            text("DELETE FROM api_tokens WHERE user_id = :uid").bindparams(
                uid=user.id
            )
        )
        await session.commit()
        return user


@pytest_asyncio.fixture
async def another_user() -> User:
    """Return (creating if needed) a second non-admin user.

    Used by ``test_revoke_other_users_token_returns_false`` to prove a
    user cannot revoke a token they don't own. Tokens are purged on
    each yield so cross-test state does not bleed.
    """
    from sqlalchemy import text

    username = "grpc-test-user-other"
    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(
                select(User).where(User.username == username)
            )
        ).scalars().first()
        if user is None:
            user = User(
                tenant_id=_DEFAULT_TENANT_ID,
                username=username,
                role=UserRole.BOOKKEEPER.value,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        await session.execute(
            text("DELETE FROM api_tokens WHERE user_id = :uid").bindparams(
                uid=user.id
            )
        )
        await session.commit()
        return user
