"""wizard_state table for multi-step import (and other) wizards.

Revision ID: 0089
Revises: 0088_paperless_webhook_secrets
Create Date: 2026-05-04
"""
from __future__ import annotations

from alembic import op

revision = "0089"
down_revision = "0088_paperless_webhook_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE wizard_state (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL,
            kind TEXT NOT NULL,
            state JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '1 hour')
        )
    """)
    op.execute(
        "CREATE INDEX wizard_state_tenant_kind_idx ON wizard_state(tenant_id, kind)"
    )
    op.execute(
        "CREATE INDEX wizard_state_expires_idx ON wizard_state(expires_at)"
    )
    op.execute("ALTER TABLE wizard_state ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY wizard_state_tenant_isolation ON wizard_state
            USING (tenant_id = current_setting('app.current_tenant')::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS wizard_state_tenant_isolation ON wizard_state")
    op.execute("DROP TABLE IF EXISTS wizard_state")
