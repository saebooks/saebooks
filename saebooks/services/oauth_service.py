"""OAuth2 service — handle provider authentication and account linking.

Handles:
- CSRF state token generation and verification
- Authorization code exchange for access tokens
- User info fetching from providers
- Account creation/linking based on OAuth identity
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.oauth import OAuthProvider, OAuthProviderLink
from saebooks.models.user import User, UserRole

logger = logging.getLogger("saebooks.oauth")


class OAuthError(Exception):
    """Base exception for OAuth2 errors."""


class OAuthProviderNotConfigured(OAuthError):
    """Provider client_id/secret not configured."""


class OAuthStateMismatch(OAuthError):
    """State token doesn't match — possible CSRF attack."""


class OAuthUserNotFound(OAuthError):
    """User email not found in OAuth provider response."""


async def _get_redis_client():
    """Get a Redis client from settings."""
    try:
        import aioredis
    except ImportError:
        raise OAuthError("aioredis not installed; install with 'pip install aioredis'")

    # Parse SAEBOOKS_OAUTH_STATE_STORE URL
    store_url = settings.oauth_state_store
    if store_url == "memory":
        # In-memory dict for testing (not thread-safe)
        logger.warning("Using in-memory state store — testing only!")
        return None

    # Parse redis://host:port/db
    parsed = urlparse(store_url)
    if parsed.scheme != "redis":
        raise OAuthError(f"Unsupported state store: {parsed.scheme}")

    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    db = int(parsed.path.lstrip("/") or "0")
    return await aioredis.create_redis_pool(f"redis://{host}:{port}/{db}")


_memory_state_store: dict[str, dict[str, Any]] = {}


async def _store_state(state_token: str, data: dict[str, Any]) -> None:
    """Store CSRF state token in Redis or memory."""
    if settings.oauth_state_store == "memory":
        _memory_state_store[state_token] = data
        return

    redis = await _get_redis_client()
    if redis:
        import json

        await redis.setex(
            f"oauth_state:{state_token}",
            settings.oauth_state_ttl_seconds,
            json.dumps(data),
        )


async def _retrieve_state(state_token: str) -> dict[str, Any] | None:
    """Retrieve and delete CSRF state token."""
    if settings.oauth_state_store == "memory":
        return _memory_state_store.pop(state_token, None)

    redis = await _get_redis_client()
    if redis:
        import json

        data = await redis.get(f"oauth_state:{state_token}")
        if data:
            await redis.delete(f"oauth_state:{state_token}")
            return json.loads(data)
    return None


def _get_provider_config(provider: str) -> dict[str, Any]:
    """Get OAuth2 config for a provider."""
    provider = provider.lower()

    if provider == "github":
        if not settings.github_client_id:
            raise OAuthProviderNotConfigured("GitHub not configured")
        return {
            "client_id": settings.github_client_id,
            "client_secret": settings.github_client_secret,
            "authorize_url": settings.github_authorize_url,
            "token_url": settings.github_token_url,
            "userinfo_url": settings.github_userinfo_url,
            "scope": settings.github_scope,
        }

    elif provider == "microsoft":
        if not settings.microsoft_client_id:
            raise OAuthProviderNotConfigured("Microsoft not configured")
        tenant = settings.microsoft_tenant
        return {
            "client_id": settings.microsoft_client_id,
            "client_secret": settings.microsoft_client_secret,
            "authorize_url": settings.microsoft_authorize_url.format(tenant=tenant),
            "token_url": settings.microsoft_token_url.format(tenant=tenant),
            "userinfo_url": settings.microsoft_userinfo_url,
            "scope": settings.microsoft_scope,
        }

    elif provider == "google":
        if not settings.google_client_id:
            raise OAuthProviderNotConfigured("Google not configured")
        return {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "authorize_url": settings.google_authorize_url,
            "token_url": settings.google_token_url,
            "userinfo_url": settings.google_userinfo_url,
            "scope": settings.google_scope,
        }

    raise OAuthError(f"Unknown provider: {provider}")


def get_authorize_url(provider: str, redirect_uri: str) -> tuple[str, str]:
    """Generate authorization URL for a provider.

    Returns (authorize_url, state_token).
    State token must be stored server-side for verification on callback.
    """
    config = _get_provider_config(provider)
    state_token = secrets.token_urlsafe(32)

    params = {
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": config["scope"],
        "state": state_token,
    }

    # Provider-specific tweaks
    if provider.lower() == "github":
        params.pop("response_type")  # GitHub doesn't need this
        params["allow_signup"] = "true"
    elif provider.lower() == "microsoft":
        params["response_mode"] = "query"
        params.pop("response_type")  # Microsoft doesn't need this

    authorize_url = f"{config['authorize_url']}?{urlencode(params)}"
    return authorize_url, state_token


async def exchange_code(
    provider: str, code: str, state: str, redirect_uri: str, stored_state: dict[str, Any]
) -> tuple[str, str, str]:
    """Exchange authorization code for user email.

    Returns (provider_user_id, email, display_name).
    Raises OAuthStateMismatch if state doesn't match (CSRF protection).
    Raises OAuthError on any provider error.
    """
    if stored_state.get("state") != state:
        raise OAuthStateMismatch("State token mismatch — possible CSRF attack")

    config = _get_provider_config(provider)

    # Exchange code for access token
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
    }

    async with httpx.AsyncClient() as client:
        token_response = await client.post(config["token_url"], data=token_data)
        token_response.raise_for_status()
        token_json = token_response.json()

    access_token = token_json.get("access_token")
    if not access_token:
        raise OAuthError(f"{provider}: No access token in response")

    # Fetch user info
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    if provider.lower() == "github":
        headers["X-GitHub-Api-Version"] = "2022-11-28"

    async with httpx.AsyncClient() as client:
        userinfo_response = await client.get(
            config["userinfo_url"],
            headers=headers,
        )
        userinfo_response.raise_for_status()
        userinfo = userinfo_response.json()

    # Extract provider-specific fields
    provider_lower = provider.lower()
    if provider_lower == "github":
        provider_user_id = str(userinfo.get("id"))
        email = userinfo.get("email")
        display_name = userinfo.get("name") or userinfo.get("login")
    elif provider_lower == "microsoft":
        provider_user_id = userinfo.get("id")
        email = userinfo.get("userPrincipalName") or userinfo.get("mail")
        display_name = userinfo.get("displayName")
    elif provider_lower == "google":
        provider_user_id = userinfo.get("sub")
        email = userinfo.get("email")
        display_name = userinfo.get("name")
    else:
        raise OAuthError(f"Unknown provider: {provider}")

    if not provider_user_id:
        raise OAuthError(f"{provider}: No user ID in response")

    if not email:
        raise OAuthUserNotFound(f"{provider}: User email not available")

    return provider_user_id, email, display_name


async def find_or_create_user(
    provider: str,
    provider_user_id: str,
    email: str,
    display_name: str | None = None,
) -> User:
    """Find existing user or create new one based on OAuth identity.

    Lookup order:
    1. Check if OAuthProviderLink exists for this provider+user_id → return user
    2. Check if email exists in users table → create link → return user
    3. Create new user with role=CLIENT → create link → return user
    """
    async with AsyncSessionLocal() as session:
        # Check for existing link
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

        # Check for existing email
        user_result = await session.execute(
            select(User).where(User.email == email)
        )
        existing_user = user_result.scalars().first()

        if existing_user and not existing_user.archived_at:
            # Link this provider to the existing user
            link = OAuthProviderLink(
                user_id=existing_user.id,
                provider=provider,
                provider_user_id=provider_user_id,
                provider_user_email=email,
            )
            session.add(link)
            await session.commit()
            return existing_user

        # Create new user
        new_user = User(
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            username=email.split("@")[0],  # Initial username from email
            display_name=display_name,
            email=email,
            role=UserRole.VIEWER.value,
            email_verified_at=datetime.now(UTC),  # Trust provider's email
        )
        session.add(new_user)
        await session.flush()

        # Create provider link
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
