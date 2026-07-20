"""External-identity service — link DiscourseConnect users to SAE Books.

Identity has been consolidated on discourse.saebooks.com.au; the DiscourseConnect
handshake itself runs in saebooks-web. This module exists solely to translate
``(provider, provider_user_id, email)`` from the post-handshake handoff into a
SAE Books ``User`` row, creating it on first contact and linking subsequent
logins by email.

The ``provider`` argument is kept generic (``String(16)`` on the link table) so
a future identity provider can be added without a schema change.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.oauth import OAuthProviderLink
from saebooks.models.user import User, UserRole

logger = logging.getLogger("saebooks.oauth")


class OAuthError(Exception):
    """Base exception for identity-handoff errors."""


async def find_linked_user(
    provider: str,
    provider_user_id: str,
    email: str,
) -> User | None:
    """Lookup-only variant of :func:`find_or_create_user` — NEVER creates.

    Fail-closed path for assertion-style providers (Estonian eID): a
    validated external assertion may only log into a user that already
    exists — matched by provider link first, then by email (which also
    upserts the link for next time). A miss returns ``None`` and the
    caller refuses; auto-creating an account from an eID assertion is a
    business decision that has deliberately NOT been taken.
    """
    import os
    DEFAULT_TENANT_ID = os.environ.get(
        "SAEBOOKS_DEFAULT_TENANT_ID",
        "00000000-0000-0000-0000-000000000001",
    )
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = DEFAULT_TENANT_ID
        link = await session.execute(
            select(OAuthProviderLink).where(
                (OAuthProviderLink.provider == provider)
                & (OAuthProviderLink.provider_user_id == provider_user_id)
            )
        )
        existing_link = link.scalars().first()
        if existing_link:
            user = await session.get(User, existing_link.user_id)
            if user and not user.archived_at:
                return user

        user_result = await session.execute(
            select(User).where(User.email == email)
        )
        existing_user = user_result.scalars().first()
        if existing_user is None or existing_user.archived_at:
            return None

        existing_link_q = await session.execute(
            select(OAuthProviderLink).where(
                (OAuthProviderLink.user_id == existing_user.id)
                & (OAuthProviderLink.provider == provider)
            )
        )
        user_link = existing_link_q.scalars().first()
        if user_link is None:
            session.add(
                OAuthProviderLink(
                    user_id=existing_user.id,
                    provider=provider,
                    provider_user_id=provider_user_id,
                    provider_user_email=email,
                )
            )
        else:
            user_link.provider_user_id = provider_user_id
            user_link.provider_user_email = email
        await session.commit()
        return existing_user


async def find_or_create_user(
    provider: str,
    provider_user_id: str,
    email: str,
    display_name: str | None = None,
) -> User:
    """Find existing user or create new one based on external identity.

    Lookup order:
    1. Existing OAuthProviderLink for this (provider, provider_user_id) → user
    2. Existing User with matching email → upsert link → user
    3. Create new User (role=VIEWER) + link → user
    """
    # NB: this is the only writer that creates rows in tenants other
    # than the implicit "current request tenant". When called from the
    # oauth-handoff path there is no request-scoped JWT yet, so we
    # bind app.current_tenant ourselves. ``SAEBOOKS_DEFAULT_TENANT_ID``
    # lets a private deploy point first-logins at its non-seed tenant
    # (e.g. books.primary routes them to the Example Pty Ltd tenant rather
    # than the seed); fallback to the legacy default keeps the public
    # / community single-tenant build working.
    import os
    DEFAULT_TENANT_ID = os.environ.get(
        "SAEBOOKS_DEFAULT_TENANT_ID",
        "00000000-0000-0000-0000-000000000001",
    )
    async with AsyncSessionLocal() as session:
        # Stamp the tenant onto session.info BEFORE the first transaction
        # begins so the after_begin listener (registered in api/v1/deps.py)
        # issues SET LOCAL app.current_tenant on every BEGIN. Without this
        # any INSERT into FORCE-RLS tables (users, oauth_provider_links)
        # raises InsufficientPrivilegeError.
        session.info["tenant_id"] = DEFAULT_TENANT_ID
        link = await session.execute(
            select(OAuthProviderLink).where(
                (OAuthProviderLink.provider == provider)
                & (OAuthProviderLink.provider_user_id == provider_user_id)
            )
        )
        existing_link = link.scalars().first()
        if existing_link:
            user = await session.get(User, existing_link.user_id)
            if user and not user.archived_at:
                return user

        user_result = await session.execute(
            select(User).where(User.email == email)
        )
        existing_user = user_result.scalars().first()

        if existing_user and not existing_user.archived_at:
            # uq_user_provider enforces one link per (user, provider). If a
            # link already exists for that pair, refresh in place rather than
            # inserting a duplicate.
            existing_link_q = await session.execute(
                select(OAuthProviderLink).where(
                    (OAuthProviderLink.user_id == existing_user.id)
                    & (OAuthProviderLink.provider == provider)
                )
            )
            user_link = existing_link_q.scalars().first()
            if user_link is None:
                session.add(
                    OAuthProviderLink(
                        user_id=existing_user.id,
                        provider=provider,
                        provider_user_id=provider_user_id,
                        provider_user_email=email,
                    )
                )
            else:
                user_link.provider_user_id = provider_user_id
                user_link.provider_user_email = email
            await session.commit()
            return existing_user

        new_user = User(
            tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
            username=email.split("@")[0],
            display_name=display_name,
            email=email,
            role=UserRole.VIEWER.value,
            email_verified_at=datetime.now(UTC),
        )
        session.add(new_user)
        await session.flush()

        link = OAuthProviderLink(
            user_id=new_user.id,
            provider=provider,
            provider_user_id=provider_user_id,
            provider_user_email=email,
        )
        session.add(link)
        await session.commit()
        await session.refresh(new_user)
        return new_user
