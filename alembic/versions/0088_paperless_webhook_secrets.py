"""Add paperless_webhook_secrets table for per-tenant inbound HMAC keys.

Each tenant that connects Paperless-ngx to SAE Books can configure one
or more webhook secrets. Inbound POSTs to
``/api/v1/integrations/paperless/webhook`` are verified against the
per-tenant secret stored here (encrypted via Fernet, same as SISS
credentials).

Security model (RLS Class A — direct tenant_id column):

* ``tenant_id`` is a NOT NULL FK to ``tenants(id)``; every row is
  owned by exactly one tenant.
* The ``tenant_isolation`` RLS policy matches migration 0055's shape
  verbatim — ``USING (tenant_id = current_setting('app.current_tenant',
  true)::uuid)`` — so cross-tenant reads/writes are blocked at the DB
  layer even when the caller bypasses the router-level auth.

The ``secret_ciphertext`` column stores the Fernet-encrypted secret
returned by ``services.crypto.encrypt_field``. The column is BYTEA
(binary) even though Fernet tokens are ASCII-safe; BYTEA is the
canonical Postgres type for opaque binary blobs and is more correct
than TEXT here — the application layer always decodes to/from bytes
via standard library routines and never interprets the ciphertext as
UTF-8 text.

Revision ID: 0088_paperless_webhook_secrets
Revises: 0087_stripe_connect (or whatever the current head is)
Create Date: 2026-05-04
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0088_paperless_webhook_secrets"
# NOTE: rollup must wire the correct down_revision once the full Cat-C
# migration chain is assembled. Set to the current prod head 0085 so
# this branch is independently testable on a clean DB.
down_revision: str | None = "0085_close_remaining_rls_gaps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Reuse 0055 predicate verbatim — one policy shape for the whole DB.
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING


def upgrade() -> None:
    op.create_table(
        "paperless_webhook_secrets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "label",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "secret_ciphertext",
            sa.LargeBinary(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_index(
        "paperless_webhook_secrets_tenant_idx",
        "paperless_webhook_secrets",
        ["tenant_id"],
    )

    # RLS Class A — direct tenant_id column.
    op.execute(
        sa.text("ALTER TABLE paperless_webhook_secrets ENABLE ROW LEVEL SECURITY")
    )
    op.execute(
        sa.text("ALTER TABLE paperless_webhook_secrets FORCE ROW LEVEL SECURITY")
    )
    op.execute(
        sa.text("DROP POLICY IF EXISTS tenant_isolation ON paperless_webhook_secrets")
    )
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON paperless_webhook_secrets "
            f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP POLICY IF EXISTS tenant_isolation ON paperless_webhook_secrets"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE paperless_webhook_secrets NO FORCE ROW LEVEL SECURITY"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE paperless_webhook_secrets DISABLE ROW LEVEL SECURITY"
        )
    )
    op.drop_index(
        "paperless_webhook_secrets_tenant_idx",
        table_name="paperless_webhook_secrets",
    )
    op.drop_table("paperless_webhook_secrets")
