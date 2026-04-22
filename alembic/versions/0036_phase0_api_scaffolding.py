"""Phase 0 API scaffolding — change_log, idempotency_keys, contact.version

Supports the API-first rebuild (Phase 0 POC + Phase 4.5 offline sync).

* ``change_log`` — append-only stream of every write through the new
  JSON API. ``id`` is a BIGSERIAL cursor; clients pull changes since a
  known id.
* ``idempotency_keys`` — server-side dedup of retried writes. Clients
  send ``X-Idempotency-Key: <uuid>``; replayed requests return the
  cached response body + status instead of re-executing.
* ``contacts.version`` — monotonic integer that increments on every
  update. The API enforces ``If-Match: <version>`` on update/delete.
  Backfilled to 1 for all existing rows in one transaction.

Revision ID: 0036_phase0_api_scaffolding
Revises: 0035_ato_sbr_config
Create Date: 2026-04-22
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_phase0_api_scaffolding"
down_revision: str | None = "0035_ato_sbr_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column(
            "entity_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    # Primary key already indexes id; add an entity-scoped covering index
    # so "changes since N for contacts" stays fast once the table grows.
    op.create_index(
        "ix_change_log_entity_id",
        "change_log",
        ["entity", "id"],
    )

    op.create_table(
        "idempotency_keys",
        sa.Column(
            "key",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "response_body",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("response_status", sa.Integer(), nullable=False),
    )

    # Add version column to contacts, backfill existing rows to 1 in the
    # same transaction, then drop the server_default so inserts must set
    # it explicitly from the ORM (keeps test_seed et al. honest).
    op.add_column(
        "contacts",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.execute("UPDATE contacts SET version = 1 WHERE version IS NULL")
    op.alter_column("contacts", "version", server_default=None)


def downgrade() -> None:
    op.drop_column("contacts", "version")
    op.drop_table("idempotency_keys")
    op.drop_index("ix_change_log_entity_id", table_name="change_log")
    op.drop_table("change_log")
