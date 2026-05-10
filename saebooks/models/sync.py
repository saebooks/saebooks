"""ORM mappings for Build #9 — accounting-package sync state tables.

Tables defined in ``alembic/versions/0095_sync_state_tables.py``:

* ``SyncConnection`` (``sync_connections``) — one row per (tenant,
  provider, external org). Owns the OAuth credential ciphertext.
* ``SyncState`` (``sync_state``) — one row per synced object on each
  connection. Carries last-pulled-etag and last-pushed-version for the
  LWW conflict detector.
* ``SyncAuditLog`` (``sync_audit_log``) — append-only log of sync
  worker activity. Distinct from ``audit_log`` (which records *user*
  actions).
* ``SyncCoaAccountRequest`` (``sync_coa_account_request``) — rate-limit
  ledger for the trigger-on-miss CoA resolver (60s per
  ``(tenant, provider, external_account_code)``).

None of these are ``CompanyScoped`` — they are tenant-scoped directly.
A single tenant's sync connection often spans multiple companies, and
the upstream provider is reasoning about *the whole tenant's* shape,
not one company's slice. The Class-A RLS policy (mig 0095) enforces
``tenant_id = app.current_tenant`` at the DB layer.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class SyncProvider(enum.StrEnum):
    """Recognised upstream providers.

    Stored as ``TEXT`` in the DB so adding a fourth provider does not
    need ``ALTER TYPE``. The string form is canonical: ``connection.provider``
    is compared as a plain string everywhere.
    """

    XERO = "xero"
    MYOB = "myob"
    QBO = "qbo"


class SyncConnectionStatus(enum.StrEnum):
    """Lifecycle of one sync connection.

    Mirrors the relay-side patterns we already use for bank-feeds
    (``BankFeedExternalCredStatus``).
    """

    PENDING_CONSENT = "pending_consent"
    ACTIVE = "active"
    ERROR = "error"
    REVOKED = "revoked"


class SyncDirection(enum.StrEnum):
    """Direction tag used by ``SyncAuditLog`` rows.

    Push and pull are the two normal flows. ``CONFLICT`` is logged
    once per detected divergence; ``CONNECT`` / ``DISCONNECT`` mark
    lifecycle events on the connection itself (consent grant /
    revoke).
    """

    PULL = "pull"
    PUSH = "push"
    CONFLICT = "conflict"
    CONNECT = "connect"
    DISCONNECT = "disconnect"


class SyncStateOrigin(enum.StrEnum):
    """Provenance tag on a ``sync_state`` row — drives push eligibility.

    Migration 0096 adds the column. The three values are mutually
    exclusive and form a small state machine:

    * ``LOCAL``  — row was created locally and never pushed; the next
      push pass will pick it up via ``last_pushed_version IS NULL``.
    * ``REMOTE`` — row was pulled from upstream. Push pass must NOT
      pick it up unless the local copy has been edited since pull
      (detected via ``version > 1`` on the underlying object — the
      object's ``version`` column starts at 1 on insert and only bumps
      on local writes).
    * ``SYNCED`` — has been successfully pushed at least once. Push
      pass picks it up iff ``version > last_pushed_version``.

    Transitions::

        LOCAL  --(first successful push)--> SYNCED
        REMOTE --(first successful push)--> SYNCED
        SYNCED stays SYNCED across subsequent push/pull cycles.

    The CHECK constraint at the DB layer enforces these three values;
    do not introduce a fourth without a migration.
    """

    LOCAL = "local"
    REMOTE = "remote"
    SYNCED = "synced"


class SyncObjectType(enum.StrEnum):
    """Object types we sync.

    A subset of our schema — bank-statement-lines, fixed assets, and
    payroll never round-trip (different shape across providers, see
    plan § "Out of scope").
    """

    CONTACT = "contact"
    INVOICE = "invoice"
    BILL = "bill"
    PAYMENT = "payment"
    CREDIT_NOTE = "credit_note"
    JOURNAL_ENTRY = "journal_entry"


class SyncConnection(Base):
    """One OAuth connection to an external accounting package.

    Holds the customer's own OAuth ``client_id``/``client_secret``
    (Fernet-encrypted, per plan §11.a.1 — Enterprise customers register
    their own apps with each provider). Also holds the long-lived
    refresh token; access tokens are not persisted (regenerated on
    demand from the refresh token, kept in the process-local
    ``XeroTokenCache``).

    Status flow::

        pending_consent --(operator clicks Connect, exchanges code)--> active
        active          --(refresh fails)----------------------------> error
        active          --(operator clicks Disconnect)---------------> revoked
        error           --(operator re-OAuths)-----------------------> active
    """

    __tablename__ = "sync_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_tenant_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_tenant_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    oauth_client_id_ciphertext: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    oauth_client_secret_ciphertext: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    oauth_refresh_token_ciphertext: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    oauth_scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=SyncConnectionStatus.PENDING_CONSENT.value,
        server_default=SyncConnectionStatus.PENDING_CONSENT.value,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_pulled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_pushed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SyncState(Base):
    """Per-object sync state for one connection.

    The conflict detector reads ``last_pulled_etag`` (provider-side
    version) and ``last_pushed_version`` (our optimistic-locking
    version at the moment of last push). When both have moved since the
    last successful exchange, that is a conflict.
    """

    __tablename__ = "sync_state"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sync_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    local_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    last_pulled_etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_pulled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_pushed_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    last_pushed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # See ``SyncStateOrigin`` and migration 0096 for semantics. Stored
    # as TEXT (CHECK-constrained) rather than ENUM so adding a fourth
    # value down the line is a no-DDL change to the CHECK predicate.
    origin: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=SyncStateOrigin.LOCAL.value,
    )

    quarantined: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SyncAuditLog(Base):
    """Append-only journal of sync worker activity."""

    __tablename__ = "sync_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sync_connections.id", ondelete="CASCADE"),
        nullable=True,
    )
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class SyncCoaAccountRequest(Base):
    """Rate-limit ledger for trigger-on-miss CoA resolver.

    Plan §11.a.5 — when the worker encounters an external account code
    that doesn't map to any local account, it requests a CoA re-pull,
    rate-limited to one request per 60 seconds per
    ``(tenant, provider, external_account_code)`` so a flood of mapped
    txns doesn't trigger a flood of re-pulls.
    """

    __tablename__ = "sync_coa_account_request"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_account_code: Mapped[str] = mapped_column(Text, nullable=False)
    last_request_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
