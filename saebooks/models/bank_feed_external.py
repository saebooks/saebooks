"""ORM mapping for ``bank_feed_external_creds`` (mig 0086).

Local mirror row for one consent flow on the feeds-server relay. See
the migration docstring for the why; the table itself lives in
``alembic/versions/0086_bank_feed_external_creds.py``.

This model is a peer of ``BankFeedClient`` (mig 0029) — they coexist:
``BankFeedClient`` is the SISS-direct legacy state, and
``BankFeedExternalCred`` is the new relay-driven state. Both can be
present in the same DB without conflict because they describe
different code paths (legacy ``saebooks/routers/bank_feeds.py`` vs new
``saebooks/api/v1/bank_feeds.py``).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class BankFeedExternalCredStatus(enum.StrEnum):
    """Status of one local mirror row.

    Mirrors the relay-side connection lifecycle, with one extra value
    (``ERROR``) reserved for "the relay accepted the consent flow but
    the upstream returned an error we don't know how to recover from
    automatically — surface it in the admin UI".
    """

    PENDING_CONSENT = "pending_consent"
    ACTIVE = "active"
    REVOKED = "revoked"
    ERROR = "error"


class BankFeedExternalCred(Base):
    """One consent flow's local mirror row.

    Not ``CompanyScoped`` — this table is **tenant-scoped** directly,
    matching the Class-A RLS shape (mig 0086). The same tenant can hold
    connections that span multiple companies, and the relay does not
    track ``company_id``; we therefore key on ``tenant_id`` directly
    rather than threading through ``companies``.

    ``account_id`` is a logical FK to the chart-of-accounts row that
    ingested transactions post into. Nullable because the user may map
    after consent initiation (see migration docstring). No DB-level FK
    by intent — same shape as ``BankFeedAccount.ledger_account_id``.
    """

    __tablename__ = "bank_feed_external_creds"

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

    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )

    # Opaque identifier issued by the upstream — relay-side this is the
    # ``connection_id`` returned by ``POST /api/v1/connections``. Named
    # ``siss_client_id`` because the historical column shape and ops
    # tooling already grep for this name; the value carries the same
    # semantic ("the upstream's handle for this consent flow").
    siss_client_id: Mapped[str] = mapped_column(Text, nullable=False)

    last_sync_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)

    # TEXT not native ENUM — see migration docstring for why.
    status: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
