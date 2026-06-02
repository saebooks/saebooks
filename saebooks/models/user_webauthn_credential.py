"""WebAuthn / FIDO2 credential model.

One row per hardware key (or platform authenticator / passkey) enrolled on
a user's account. Created by ``/api/v1/auth/webauthn/register/finish`` and
read at every ``/api/v1/auth/webauthn/authenticate/finish``.

Notes
-----
- ``credential_id`` is unique globally — it's a cryptographically-random
  binary blob assigned by the authenticator at registration. The unique
  constraint lets us look up the row directly from a WebAuthn assertion.
- ``sign_count`` is the anti-replay counter. We bump it on every successful
  authentication; if the authenticator ever returns a count <= what we
  have stored, the assertion is rejected as a replay.
- ``aaguid`` is the 16-byte authenticator-model identifier. Useful for
  displaying e.g. "YubiKey 5 NFC" to the user in their settings page.
- Discoverable-credential login (passkey) needs to look up by
  credential_id without a tenant context. Migration 0135 provides
  ``webauthn_lookup_credential(bytea)`` SECURITY DEFINER for that.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import BigInteger, DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class UserWebauthnCredential(Base):
    __tablename__ = "user_webauthn_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    transports: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), nullable=False, server_default=sa.text("ARRAY[]::varchar[]"),
    )
    aaguid: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    friendly_name: Mapped[str] = mapped_column(String(64), nullable=False, server_default="Security key")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
