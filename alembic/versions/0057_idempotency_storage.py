"""Race-safe idempotency storage — RFC 8417 compliant.

Background
----------
The original ``idempotency_keys`` table (Phase 0) used ``session.get()``
followed by ``session.add()`` in a non-atomic read-modify-write.  Under
concurrent load (10 parallel POSTs with the same key) the race window
between the SELECT and the INSERT produced 9 IntegrityError / UniqueViolation
exceptions that bubbled to the caller as HTTP 500.

Additionally, there was no per-tenant scoping and no body-hash, so:

* The same idempotency key from two different tenants would collide at the
  DB level (first writer wins across tenants — subtle data leak).
* A replay with a *different* body silently returned the original resource
  instead of the RFC 8417-required 422.

Resolution
----------
New table ``idempotency_records`` with:

* ``idempotency_key TEXT PRIMARY KEY`` — gives the UNIQUE constraint that
  the ON CONFLICT clause requires.
* ``tenant_id UUID NOT NULL`` — scopes the record to a tenant so cross-tenant
  key collisions are resolved at the service layer.
* ``body_sha256 TEXT NOT NULL`` — SHA-256 hex digest of the raw request body.
  Replays must present the same digest or receive a 422.
* ``response_status INTEGER NOT NULL`` — HTTP status code of the original
  response.
* ``response_body BYTEA NOT NULL`` — serialised JSON response body.
* ``created_at TIMESTAMPTZ NOT NULL DEFAULT now()`` — for the 7-day
  retention sweep (Phase 1 cycle 4).

The PRIMARY KEY on ``idempotency_key`` gives the unique constraint for
free — no separate UNIQUE index needed.

Race-safe write pattern (in saebooks/services/idempotency.py)
------------------------------------------------------------
We use::

    INSERT INTO idempotency_records (idempotency_key, tenant_id, body_sha256, ...)
    VALUES (:key, :tenant_id, :sha256, ...)
    ON CONFLICT (idempotency_key)
    DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
    RETURNING *

The ``DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key`` is a no-op
update (the PK never actually changes) but it forces PostgreSQL to always
fire RETURNING — even on conflict.  This gives us the existing row in both
the "first writer" and "later writers" code path without a second SELECT.

The service then checks whether the returned row's ``body_sha256`` matches
the caller's hash:
* Match → return cached response (idempotent replay).
* Mismatch → return 422 with ``idempotency_key_conflict`` error code.

The old ``idempotency_keys`` table is left in place.  The router files that
use it cannot be touched in this change set; they will be migrated to the
new service in a follow-up cleanup sprint.

Revision ID: 0057_idempotency_storage
Revises: 0056_split_db_role
Create Date: 2026-04-26
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0057_idempotency_storage"
down_revision: str | None = "0056_split_db_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body_sha256", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        # BYTEA stores the serialised JSON response body verbatim.
        sa.Column("response_body", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("idempotency_key", name="pk_idempotency_records"),
    )

    # Grant DML to the non-superuser app role so the runtime API can write.
    # Wrapping in DO $$ ... $$ makes it safe when the role doesn't exist yet
    # (e.g. development setups that haven't run migration 0056 manually).
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE
                        ON TABLE idempotency_records TO saebooks_app;
                END IF;
            END $$;
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                    REVOKE ALL ON TABLE idempotency_records FROM saebooks_app;
                END IF;
            END $$;
            """
        )
    )
    op.drop_table("idempotency_records")
