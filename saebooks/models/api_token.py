"""Machine-readable API tokens for the CLI, MCP server, and any
third-party automation that wants to talk to SAE Books without a
browser session.

These are **not** the same as the single-use email tokens in
``services/auth_tokens.py`` — those are 15min–24h SHA-256 hashes used
in verification / reset / magic-link flows. API tokens here are
long-lived secrets, bcrypt-hashed at rest, scoped to a (user, company)
tuple, with optional fine-grained scopes.

Wire format
-----------

The raw token presented over ``Authorization: Bearer ...`` is
``saebk_<64 hex>``. The ``saebk_`` prefix lets ``require_bearer``
short-circuit JWT decode for obvious API tokens. The 64-hex tail is
the random material — 32 bytes of ``secrets.token_bytes`` rendered as
lowercase hex (256 bits of entropy).

At rest we store the bcrypt hash of the full ``saebk_...`` string,
plus a six-char ``token_prefix`` (the first 6 chars after the
underscore) so the verify path can do an indexed lookup before
spending a bcrypt op.

Verification path (hot loop)
----------------------------

Bcrypt is slow by design — we can't iterate over every row on every
request. Instead, ``token_prefix`` is unique and indexed: lookup is
O(1) by prefix, then bcrypt-verify against that single row's hash.
This keeps verification at ~1 bcrypt op per request, same cost as
password login but on a hot path so we keep the cost factor modest
(work factor 10).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

if TYPE_CHECKING:
    from saebooks.models.company import Company
    from saebooks.models.user import User


class ApiToken(CompanyScoped, Base):
    """A long-lived bearer token issued to a user for machine access."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    token_prefix: Mapped[str] = mapped_column(
        String(6), nullable=False, unique=True, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(60), nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship("User", lazy="joined")
    company: Mapped["Company"] = relationship("Company", lazy="joined")

    __table_args__ = (
        Index("ix_api_tokens_company_user", "company_id", "user_id"),
        Index(
            "ix_api_tokens_active",
            "company_id",
            "user_id",
            postgresql_where="revoked_at IS NULL",
        ),
    )

    @property
    def is_active(self) -> bool:
        """True iff the token is not revoked and not expired."""
        from datetime import UTC, datetime as _dt
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at < _dt.now(UTC):
            return False
        return True
