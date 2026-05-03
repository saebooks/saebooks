"""Password-based JWT auth endpoints — B/43.

Implements:
    POST /api/v1/auth/login   — exchange email+password for an access token
    POST /api/v1/auth/refresh — renew an unexpired token
    GET  /api/v1/auth/me      — return the token owner's profile

These endpoints are deliberately kept separate from
``saebooks/api/v1/auth.py``, which provides the legacy static-bearer
dependency (``require_bearer`` / ``BearerDep``) used by all existing v1
routers. The static-bearer and JWT auth paths coexist: existing routes
keep working unchanged.

JWT payload schema
------------------
    {
        "sub": "<user_id>",          # UUID string
        "tenant_id": "<tenant_id>",  # UUID string
        "role": "<role>",
        "exp": <unix_ts>,
        "iat": <unix_ts>,
    }

The ``/auth/me`` endpoint verifies the JWT, looks up the user row, and
returns the live profile so the client always gets up-to-date role/email
even after an admin edits the user.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.user import User
from saebooks.services.jwt_tokens import JWTError, create_access_token, decode_access_token

router = APIRouter(prefix="/auth", tags=["auth"])

_ACCESS_TOKEN_TTL = 8 * 3600  # 8 hours in seconds


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class UserProfile(BaseModel):
    id: str
    username: str
    email: str | None
    name: str | None
    role: str
    tenant_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_bearer(authorization: str | None) -> str | None:
    """Pull the token string from an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


async def _user_by_email(email: str) -> User | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.email == email)
        )
        return result.scalars().first()


async def _user_by_id(user_id: str) -> User | None:
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return None
    async with AsyncSessionLocal() as session:
        return await session.get(User, uid)


def _make_token(user: User) -> TokenResponse:
    token = create_access_token(
        {
            "sub": str(user.id),
            "tenant_id": str(user.tenant_id),
            "role": user.role,
        },
        expires_in_seconds=_ACCESS_TOKEN_TTL,
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=_ACCESS_TOKEN_TTL,
    )


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest) -> TokenResponse:
    """Exchange email + password for a JWT access token.

    Returns 401 (same message) for unknown email, wrong password.
    Returns 403 if the account is archived.
    """
    from saebooks.services.jwt_tokens import (
        verify_password,
    )

    _INVALID = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    user = await _user_by_email(body.email)
    if user is None:
        # Timing-safe: do a dummy verify so we don't leak the "email not found"
        # case via response time.
        verify_password("dummy", "pbkdf2sha256$260000$0000$0000")
        raise _INVALID

    if user.password_hash is None:
        # Account exists but has no local password (OAuth-only user).
        # Run a dummy check for timing safety.
        verify_password("dummy", "pbkdf2sha256$260000$0000$0000")
        raise _INVALID

    if not verify_password(body.password, user.password_hash):
        raise _INVALID

    if user.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive",
        )

    return _make_token(user)


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    body: RefreshRequest | None = None,
    authorization: str | None = Header(default=None),
) -> TokenResponse:
    """Issue a new token for a currently-valid token.

    Accepts the current token either as ``Authorization: Bearer <token>``
    or (for clients that POST a body) as ``{"refresh_token": "<token>"}``.
    The token must not yet be expired.
    """
    raw_token = _extract_bearer(authorization)
    if raw_token is None and body is not None:
        raw_token = body.refresh_token

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_access_token(raw_token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = await _user_by_id(claims.get("sub", ""))
    if user is None or user.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return _make_token(user)


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------


@router.get("/me", response_model=UserProfile)
async def me(
    authorization: str | None = Header(default=None),
) -> UserProfile:
    """Return the profile of the authenticated user.

    Requires a valid JWT in ``Authorization: Bearer <token>``.
    Returns 401 if the token is missing, invalid, or expired.
    """
    raw_token = _extract_bearer(authorization)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_access_token(raw_token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = await _user_by_id(claims.get("sub", ""))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return UserProfile(
        id=str(user.id),
        username=user.username,
        email=user.email,
        name=user.display_name,
        role=user.role,
        tenant_id=str(user.tenant_id),
    )


# ---------------------------------------------------------------------------
# POST /auth/change-password
# ---------------------------------------------------------------------------


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password", status_code=204)
async def change_password(
    body: ChangePasswordRequest,
    authorization: str | None = Header(default=None),
) -> None:
    """Change the authenticated user's password.

    Requires a valid JWT and the correct current password.
    Returns 204 on success, 401 on bad token or wrong current password.
    """
    from saebooks.services.jwt_tokens import (
        JWTError,
        decode_access_token,
        hash_password,
        verify_password,
    )

    raw_token = _extract_bearer(authorization)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_access_token(raw_token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = await _user_by_id(claims.get("sub", ""))
    if user is None or user.archived_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if user.password_hash is None or not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")

    if len(body.new_password) < 8:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Password must be at least 8 characters")

    async with AsyncSessionLocal() as session:
        db_user = await session.get(type(user), user.id)
        db_user.password_hash = hash_password(body.new_password)
        await session.commit()


# ---------------------------------------------------------------------------
# POST /auth/oauth-handoff  (server-to-server only — saebooks-web → saebooks-api)
# ---------------------------------------------------------------------------


class OAuthHandoffRequest(BaseModel):
    provider: str
    provider_user_id: str
    email: str
    display_name: str | None = None


@router.post("/oauth-handoff", response_model=TokenResponse)
async def oauth_handoff(
    body: OAuthHandoffRequest,
    x_oauth_handoff_secret: str | None = Header(default=None, alias="X-OAuth-Handoff-Secret"),
) -> TokenResponse:
    """Internal endpoint — saebooks-web calls this after a successful OAuth
    callback to create/lookup the user and mint a JWT. The shared
    SAEBOOKS_OAUTH_HANDOFF_SECRET (in both containers' env) gates access.

    Not exposed externally — Caddy routes /api/v1/* to saebooks-web, which
    only relays calls users have authenticated for; this endpoint exists
    solely for the OAuth callback in saebooks-web to invoke directly.
    """
    import os

    expected = os.environ.get("SAEBOOKS_OAUTH_HANDOFF_SECRET", "")
    if not expected or not x_oauth_handoff_secret or x_oauth_handoff_secret != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    if body.provider.lower() not in {"github", "microsoft", "google"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown provider")
    if not body.email or "@" not in body.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid email")

    # Portal lockdown — app.saebooks.com.au is Richard's internal portal,
    # not a public SaaS. Only allow-listed emails may complete OAuth login.
    # Empty allowlist = open (dev/community); production sets it.
    from saebooks.config import settings as _settings
    allowed = _settings.oauth_allowed_emails_set
    if allowed and body.email.strip().lower() not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this portal is private — your email is not authorised",
        )

    from saebooks.services.oauth_service import find_or_create_user

    user = await find_or_create_user(
        provider=body.provider.lower(),
        provider_user_id=str(body.provider_user_id),
        email=body.email.strip().lower(),
        display_name=body.display_name,
    )
    if user.archived_at is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account inactive")

    return _make_token(user)
