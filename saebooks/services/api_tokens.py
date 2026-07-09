"""Machine-readable API tokens — issue, verify, list, revoke.

See ``models/api_token.py`` for the data model rationale. The verify
path is the hot loop: every Connect / REST / MCP request that doesn't
present a JWT comes through here. Cost target: ~1 bcrypt op per
request (lookup-by-prefix is O(1)).

Token format
------------

Cleartext (what the user sees once at issuance):
    saebk_<64 lowercase hex chars>

The ``saebk_`` prefix lets ``require_bearer`` skip JWT decode for
obvious API tokens — saves a parse on every request.

The 6 hex chars immediately after ``saebk_`` are stored verbatim as
``token_prefix`` for lookup. The full string is bcrypt-hashed (work
factor 10) into ``token_hash``.

Never call ``issue()`` outside an authenticated user context — the
returned cleartext is shown to the user ONCE and never recoverable.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.api_token import ApiToken

# Bcrypt work factor for token hashes. 10 is the standard "fast enough
# on a hot path" — ~50ms on a modern x86 core. We can lift this if we
# add caching or move to a more expensive scheme.
BCRYPT_WORK_FACTOR = 10

# 32 bytes = 64 hex chars = 256 bits of entropy. Way more than needed
# for guess-resistance; chosen to match the SHA-256 hex space used
# elsewhere for visual consistency.
TOKEN_ENTROPY_BYTES = 32

PREFIX_LEN = 6
TOKEN_PREFIX_HEADER = "saebk_"


class TokenVerifyError(Exception):
    """Raised by ``verify_token`` when the presented token is invalid,
    revoked, or expired. The API layer turns this into 401."""


def _generate_raw_token() -> tuple[str, str]:
    """Return ``(full_cleartext, lookup_prefix)``.

    The full cleartext is what gets shown to the user. The lookup
    prefix is what gets stored in ``token_prefix`` for the verify path.
    """
    hex_body = secrets.token_bytes(TOKEN_ENTROPY_BYTES).hex()
    full = TOKEN_PREFIX_HEADER + hex_body
    lookup_prefix = hex_body[:PREFIX_LEN]
    return full, lookup_prefix


async def issue(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    company_id: uuid.UUID,
    name: str,
    scopes: list[str] | None = None,
    ttl_days: int | None = None,
) -> tuple[ApiToken, str]:
    """Mint a new API token for ``user_id`` scoped to ``company_id``.

    Returns ``(ApiToken row, cleartext)`` — the cleartext is shown to
    the user exactly once and then thrown away. The caller is
    responsible for committing the session.
    """
    if not name.strip():
        raise ValueError("API token name must not be empty")

    cleartext, lookup_prefix = _generate_raw_token()
    token_hash = bcrypt.hashpw(
        cleartext.encode("utf-8"),
        bcrypt.gensalt(BCRYPT_WORK_FACTOR),
    ).decode("ascii")

    expires_at: datetime | None = None
    if ttl_days is not None and ttl_days > 0:
        expires_at = datetime.now(UTC) + timedelta(days=ttl_days)

    token = ApiToken(
        id=uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        name=name.strip(),
        token_prefix=lookup_prefix,
        token_hash=token_hash,
        scopes=scopes or [],
        created_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    session.add(token)
    await session.flush()
    return token, cleartext


async def verify(
    session: AsyncSession,
    presented: str,
) -> ApiToken:
    """Resolve a presented ``saebk_*`` bearer to an active ApiToken row.

    Raises ``TokenVerifyError`` for any failure mode — caller turns
    that into 401. Updates ``last_used_at`` on success (without
    awaiting flush — the request handler's commit takes care of it).
    """
    if not presented.startswith(TOKEN_PREFIX_HEADER):
        raise TokenVerifyError("not an api token")
    hex_body = presented[len(TOKEN_PREFIX_HEADER):]
    if len(hex_body) != TOKEN_ENTROPY_BYTES * 2:
        raise TokenVerifyError("malformed api token")

    lookup_prefix = hex_body[:PREFIX_LEN]

    # Unique constraint on token_prefix means at most one row.
    row = (
        await session.execute(
            select(ApiToken).where(ApiToken.token_prefix == lookup_prefix)
        )
    ).scalar_one_or_none()
    if row is None:
        raise TokenVerifyError("unknown api token")

    if not row.is_active:
        raise TokenVerifyError("api token revoked or expired")

    if not bcrypt.checkpw(
        presented.encode("utf-8"),
        row.token_hash.encode("ascii"),
    ):
        raise TokenVerifyError("api token hash mismatch")

    row.last_used_at = datetime.now(UTC)
    return row


async def list_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    company_id: uuid.UUID,
    include_revoked: bool = False,
) -> list[ApiToken]:
    """List the user's tokens, newest first."""
    query = (
        select(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.company_id == company_id)
        .order_by(ApiToken.created_at.desc())
    )
    if not include_revoked:
        query = query.where(ApiToken.revoked_at.is_(None))
    return list((await session.execute(query)).scalars().all())


async def revoke(
    session: AsyncSession,
    *,
    token_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Mark a token revoked. Returns True if a row was changed.

    Bound to ``user_id`` for safety — a user can only revoke their
    own tokens. Admin revocation goes through a separate code path
    that doesn't pass user_id.
    """
    row = (
        await session.execute(
            select(ApiToken).where(
                ApiToken.id == token_id, ApiToken.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(UTC)
    return True


def to_public_dict(token: ApiToken, *, cleartext: str | None = None) -> dict[str, Any]:
    """Serialise an ApiToken for API responses. Includes cleartext on
    creation only — never on subsequent listings."""
    payload: dict[str, Any] = {
        "id": str(token.id),
        "name": token.name,
        "token_prefix": token.token_prefix,
        "scopes": token.scopes,
        "created_at": token.created_at.isoformat() if token.created_at else None,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
        "active": token.is_active,
    }
    if cleartext is not None:
        payload["token"] = cleartext
        payload["warning"] = (
            "Save this token now. It will not be shown again."
        )
    return payload


__all__ = [
    "TOKEN_PREFIX_HEADER",
    "TokenVerifyError",
    "issue",
    "list_for_user",
    "revoke",
    "to_public_dict",
    "verify",
]
