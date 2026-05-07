"""OAuth2 provider link — maps external OAuth identities to SAE Books users.

When a user authenticates via GitHub/Microsoft/Google, we store the
provider's user ID and email in this table so future logins can be
matched to the existing SAE Books user without re-entering email.
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


class OAuthProvider(enum.StrEnum):
    """Supported external identity providers.

    DISCOURSE is not OAuth2 — it's DiscourseConnect (HMAC-signed nonce flow)
    handled in saebooks-web. The link row uses this same table because the
    semantics are identical: external_id + email → SAE Books user.
    """

    DISCOURSE = "discourse"


class OAuthProviderLink(Base):
    __tablename__ = "oauth_provider_links"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String(16), nullable=False
    )
    provider_user_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    provider_user_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.UniqueConstraint("user_id", "provider", name="uq_user_provider"),
        sa.UniqueConstraint("provider", "provider_user_id", name="uq_provider_id"),
    )
