"""User model — keyed on a username derived from the verified email.

Rows are created by the OAuth callback handler or the ``/api/v1/auth/signup``
endpoint, and the role is assigned by an admin via ``/admin/users/{id}``.
Until then, a newly-seen user sits at the default role (``viewer``) —
no destructive actions possible.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class UserRole(enum.StrEnum):
    """All v2 roles. Ordered from most-privileged to least.
    
    Migration 0058 renamed/collapsed: readonly -> viewer, client -> viewer.
    New hierarchy: owner > admin > accountant > bookkeeper > viewer.
    """

    OWNER = "owner"
    ADMIN = "admin"
    ACCOUNTANT = "accountant"
    BOOKKEEPER = "bookkeeper"
    VIEWER = "viewer"


# Lookup set for the middleware + authz dep to validate header values
# without reimporting the enum class everywhere.
VALID_ROLES: frozenset[str] = frozenset(r.value for r in UserRole)


# Capability hierarchy — higher number == more privileged. Used by
# ``require_role`` to allow a single admin decoration to also permit
# accountants, etc.
_ROLE_RANK: dict[str, int] = {
    UserRole.VIEWER.value: 0,
    UserRole.BOOKKEEPER.value: 1,
    UserRole.ACCOUNTANT.value: 2,
    UserRole.ADMIN.value: 3,
    UserRole.OWNER.value: 4,
}


def role_rank(role: str) -> int:
    """Return the rank for ``role`` (higher = more privileged).

    Returns ``-1`` for unknown roles so stale role strings always fail
    closed (``has_at_least`` returns False, ``require_role`` 403s).
    """
    return _ROLE_RANK.get(role, -1)


def has_at_least(user_role: str, required: str) -> bool:
    """Does ``user_role`` equal or outrank ``required``?"""
    ur = role_rank(user_role)
    rr = role_rank(required)
    if ur < 0 or rr < 0:
        return False
    return ur >= rr


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=UserRole.VIEWER.value,
        server_default=UserRole.VIEWER.value,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Per-user theme override (QQ). Null = inherit the server-wide
    # theme (SAEBOOKS_FRONTEND env or the ``theme`` row in ``settings``).
    # Only gates the CSS bundle, not the template tree.
    preferred_theme: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optimistic-locking version — added by migration 0038_phase1_user_version.
    # Starts at 1, incremented on every API write.
    version: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=1,
        server_default="1",
    )
    # Hashed password for the /auth/login endpoint (PBKDF2-HMAC-SHA256).
    # NULL means the user only has an OAuth identity (GitHub / Google /
    # Microsoft) and cannot log in via the password endpoint.
    # Added by migration 0053_user_password_hash.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # ----- 0077_user_auth_tokens — public-auth scaffolding ---------- #
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    email_verification_token_hash: Mapped[str | None] = mapped_column(
        sa.CHAR(64), nullable=True
    )
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_reset_token_hash: Mapped[str | None] = mapped_column(
        sa.CHAR(64), nullable=True
    )
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    magic_link_token_hash: Mapped[str | None] = mapped_column(
        sa.CHAR(64), nullable=True
    )
    magic_link_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bumped on every password rotation (reset, change-password). Old
    # JWTs whose ``pwv`` claim doesn't match are rejected by
    # ``require_bearer``, so a leaked token can be invalidated by the
    # user clicking "reset password". Default 0; missing claim treated
    # as 0 so JWTs minted before 0077 keep working until they expire.
    password_version: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default="0"
    )
    # ----- 0079_user_signup_plan — persist plan selection from CTA ---- #
    # Set at signup if the user arrived via a ?plan=business/pro/enterprise
    # CTA link. Cleared to NULL after email verification, at which point
    # the web layer redirects them to /billing/checkout?plan=<plan>.
    # NULL means community (no paid plan selected).
    signup_plan: Mapped[str | None] = mapped_column(
        sa.String(16), nullable=True
    )

    # ----- 0080_oauth_and_fido2 — multi-factor authentication -------- #
    # FIDO2/WebAuthn registration timestamp. NULL = user has no security keys.
    fido2_registered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Count of FIDO2 credentials registered for this user.
    # Used to determine if user can authenticate with a security key.
    fido2_credential_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default="0"
    )

    # ----- 0096_launch_promo — first-1000 Pro JWT stamp -------------- #
    # When the launch promo is active and the user claimed a slot, the
    # license-server Ed25519 JWT is cached here. The app reads this at
    # login to determine the effective edition for the session without
    # hitting the license-server on every request. NULL = no promo.
    launch_promo_jwt: Mapped[str | None] = mapped_column(
        sa.Text(), nullable=True
    )
