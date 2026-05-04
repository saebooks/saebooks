"""Force RLS on wizard_state — owner must not bypass policy.

The 0089 migration enabled RLS but did not FORCE it, which means the
table-owning role (the app's saebooks user) silently bypasses the
``wizard_state_tenant_isolation`` policy and cross-tenant reads succeed
under it. Other tenant tables (contacts, invoices, bills, etc.) all
FORCE RLS for this exact reason.

Revision ID: 0091
Revises: 0090
Create Date: 2026-05-04
"""
from __future__ import annotations

from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE wizard_state FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE wizard_state NO FORCE ROW LEVEL SECURITY")
