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
from saebooks.db import AsyncSessionLocal
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
    magic_link_url = f"{settings.oauth_redirect_uri_base}/auth/magic-link/verify/{token}"

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
        raise MagicLinkError(f"Failed to send email: {e}")

    logger.info(f"Magic link sent to {email}")
    return token


async def verify_magic_link(token: str) -> User:
    """Verify magic link token and return/create user.

    Lookup order:
    1. Find user by email from token
    2. Create user if doesn't exist (role=CLIENT, email auto-verified)
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

    async with AsyncSessionLocal() as session:
        # Try to find existing user by email
        user_result = await session.execute(
            select(User).where(User.email == email)
        )
        existing_user = user_result.scalars().first()

        if existing_user and not existing_user.archived_at:
            # Mark email as verified
            if not existing_user.email_verified_at:
                existing_user.email_verified_at = datetime.now(UTC)
                session.add(existing_user)
            # Mark token as used
            _token_store[token]["used"] = True
            await session.commit()
            return existing_user

        # Create new user
        new_user = User(
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            username=email.split("@")[0],  # Initial username from email
            email=email,
            role=UserRole.CLIENT.value,
            email_verified_at=datetime.now(UTC),  # Auto-verify via magic link
        )
        session.add(new_user)
        await session.commit()
        await session.refresh(new_user)

        # Mark token as used
        _token_store[token]["used"] = True

        logger.info(f"Created new user via magic link: {email}")
        return new_user
