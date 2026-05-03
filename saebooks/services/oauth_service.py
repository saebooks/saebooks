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
    async with AsyncSessionLocal() as session:
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
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
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
