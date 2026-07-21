"""ORM mapping for the ``wizard_state`` table (migration 0089).

Historically the multi-step import wizard (bank CSV/OFX, CoA, QBO, ATO SBR)
persisted its state through raw Postgres SQL in ``saebooks.api.v1._wizard``
(``current_setting('app.current_tenant')::uuid``, ``CAST(:state AS jsonb)``,
JSONB ``||`` merge). That SQL is Postgres-only and fails on the SQLite
Cashbook / Community backend with ``unrecognized token: ":"`` — so bank
statement import was completely broken on the free single-device edition
(``docker-compose.community.yml``) and the one-click installer.

This model exists so:

* ``saebooks.db.bootstrap_schema`` (SQLite/Community + the test harness)
  creates the ``wizard_state`` table from ORM metadata — previously no ORM
  model declared it, so the table simply did not exist on SQLite.
* ``Wizard`` can take a dialect-agnostic ORM path on SQLite (Python-side
  JSON merge, ``tenant_id`` backfilled by the ``_fill_tenant_id_on_flush``
  before-flush listener) while keeping the byte-for-byte Postgres raw-SQL
  path unchanged for production (RLS-enforced multi-tenant).

Column shapes mirror migration 0089 exactly (the alembic model-drift guard
``tests/db/test_alembic_model_drift.py`` fails on any ORM column the
migrations never created), so this adds NO Postgres migration surface — on
Postgres the table is still owned by alembic; the ORM model only maps it.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class WizardState(Base):
    __tablename__ = "wizard_state"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Migration 0089 declares ``tenant_id UUID NOT NULL`` with NO foreign key
    # (RLS, not an FK, enforces tenant scoping on Postgres); mirror that exactly
    # so the alembic model-drift guard sees no schema difference. Backfilled
    # from ``session.info['tenant_id']`` by ``_fill_tenant_id_on_flush``.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        default=_DEFAULT_TENANT,
        comment="Owning tenant — backfilled from session.info before flush.",
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
