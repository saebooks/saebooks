"""Magic Links — passwordless email-based authentication.

Generates time-limited tokens sent via email. User clicks link to auto-login without
entering a password. Works across devices (link valid on any device that receives email).
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal, LoginSessionLocal
from saebooks.models.user import User, UserRole
from saebooks.services.email import send_email

logger = logging.getLogger("saebooks.magic_link")

# In-memory store for tokens (in production, use Redis)
_token_store: dict[str, dict[str, Any]] = {}


class MagicLinkError(Exception):
    """Base exception for Magic Link errors."""


class MagicLinkTokenExpired(MagicLinkError):
    """Token has expired."""


class MagicLinkTokenInvalid(MagicLinkError):
    """Token is invalid or not found."""


async def generate_magic_link(email: str) -> str:
    """Generate a magic link token and send email.

    Returns the token (for testing).
    """
    # Verify email format
    if not email or "@" not in email:
        raise MagicLinkError("Invalid email address")

    # Generate secure random token
    token = secrets.token_urlsafe(32)

    # Store token with expiry
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    _token_store[token] = {
        "email": email.lower(),
        "expires_at": expires_at,
        "used": False,
    }

    # Build magic link URL
    magic_link_url = f"{settings.public_base_url}/auth/magic-link/verify/{token}"

    # Send email
    try:
        await send_email(
            to=email,
            subject="Your SAE Books Login Link",
            template="magic_link_email",
            context={
                "magic_link": magic_link_url,
                "expires_minutes": 15,
            },
        )
    except Exception as e:
        logger.error(f"Failed to send magic link email to {email}: {e}")
        # Remove token if email send fails
        _token_store.pop(token, None)
        raise MagicLinkError(f"Failed to send email: {e}") from e

    logger.info(f"Magic link sent to {email}")
    return token


async def verify_magic_link(token: str) -> User:
    """Verify magic link token and return/create user.

    Lookup order:
    1. Find user by email from token
    2. Create user if doesn't exist (role=VIEWER, email auto-verified)
    """
    if token not in _token_store:
        raise MagicLinkTokenInvalid("Token not found or already used")

    token_data = _token_store[token]

    # Check expiry
    if datetime.now(UTC) > token_data["expires_at"]:
        _token_store.pop(token, None)
        raise MagicLinkTokenExpired("Magic link has expired")

    # Check if already used
    if token_data.get("used"):
        raise MagicLinkTokenInvalid("Token has already been used")

    email = token_data["email"]

    # Pre-auth lookup BY EMAIL — the tenant is not known yet, so this must
    # use the BYPASSRLS owner role (LoginSessionLocal). Under the runtime
    # NOBYPASSRLS saebooks_app role, FORCE RLS on ``users`` (migration 0055)
    # would silently return zero rows for the unbound SELECT — the magic
    # link would then create a DUPLICATE user on every login instead of
    # finding the existing one. See db.py for the LoginSessionLocal rationale
    # and api/v1/login.py::_user_by_email for the reference pattern.
    async with LoginSessionLocal() as lookup_session:
        existing_user = (
            await lookup_session.execute(select(User).where(User.email == email))
        ).scalars().first()

    if existing_user is not None and not existing_user.archived_at:
        # Write path: mark email verified under the existing user's tenant.
        # Stamp session.info so the after_begin listener issues SET LOCAL
        # app.current_tenant, satisfying FORCE RLS on the UPDATE.
        async with AsyncSessionLocal() as session:
            session.info["tenant_id"] = str(existing_user.tenant_id)
            db_user = await session.get(User, existing_user.id)
            if db_user is not None and not db_user.email_verified_at:
                db_user.email_verified_at = datetime.now(UTC)
            await session.commit()
        _token_store[token]["used"] = True
        return existing_user

    # Create new user in the seed/default tenant. Bind that tenant so the
    # INSERT passes the tenant_isolation WITH CHECK under FORCE RLS.
    default_tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    async with AsyncSessionLocal() as session:
        session.info["tenant_id"] = str(default_tenant_id)
        new_user = User(
            tenant_id=default_tenant_id,
            username=email.split("@")[0],  # Initial username from email
            email=email,
            role=UserRole.VIEWER.value,
            email_verified_at=datetime.now(UTC),  # Auto-verify via magic link
        )
        session.add(new_user)
        await session.commit()
        await session.refresh(new_user)

    # Mark token as used
    _token_store[token]["used"] = True

    logger.info(f"Created new user via magic link: {email}")
    return new_user
