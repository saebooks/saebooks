"""0170_ephemeral_demo_tenants — control table for public-preview demo tenants.

Why this migration exists
-------------------------
The public preview links (app.saebooks.com.au etc.) mint a *fresh saebooks
company* (its own RLS tenant) on every visit so one visitor's scratch data
never leaks to the next, and a 60s background reaper hard-deletes the company
once it goes idle (>30m) or aged out (>2h). This table is the reaper's
worklist: one row per live ephemeral demo company.

Columns
-------
  company_id      PK + FK -> companies(id) ON DELETE CASCADE
  created_at      timestamptz NOT NULL  (provision time; age = now - created_at)
  last_seen_at    timestamptz NOT NULL  (bumped on each authed request; idle = now - last_seen_at)
  source_ip       inet NULL             (provisioning client IP; per-IP rate-limit + forensics)
  request_count   int NOT NULL DEFAULT 0 (bumped alongside last_seen_at)

DELIBERATE RLS EXEMPTION — documented, not an oversight
-------------------------------------------------------
This table is INTENTIONALLY GLOBAL — it is NOT tenant-scoped and is therefore
EXEMPT from the new-tenant-table RLS checklist (tenant_id NOT NULL + FK,
ENABLE/FORCE ROW LEVEL SECURITY, tenant_isolation policy). That exemption is by
design:

* It is a CONTROL-PLANE table, not customer ledger data. It holds no financial
  rows — only (company_id, timestamps, source_ip, request_count) bookkeeping
  the reaper needs.
* The reaper sweeps it ACROSS ALL TENANTS in one query (running with no
  app.current_tenant GUC set) to find idle / aged demos. A tenant_isolation
  policy keyed on current_setting('app.current_tenant') would hide every row
  from that cross-tenant sweep, defeating the table's only purpose.
* It carries NO tenant_id column, so there is nothing to isolate.
* The data it points at — the demo companies' rows — remain fully RLS-isolated
  as ordinary tenants (distinct tenant_id + FORCE RLS on every tenant table).
  This table sits beside that isolation; it does not weaken it.

Membership of this table is the GATE on the reaper's hard-delete: a company is
only ever hard-deleted if it has a row here, so a real company (no row) can
never be touched. See saebooks/services/ephemeral_demo.py and
saebooks/models/ephemeral_demo_tenant.py.

Additive + reversible: this migration only CREATEs a new table; it never alters
an existing tenant table. downgrade() drops it cleanly (CASCADE rows go with the
parent company anyway).

Revision ID: 0170_ephemeral_demo_tenants
Revises: 0169_company_bad_debt_settings
Create Date: 2026-06-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0170_ephemeral_demo_tenants"
down_revision: str | None = "0169_company_bad_debt_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ephemeral_demo_tenants",
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Postgres INET; renders as VARCHAR on the SQLite cashbook backend
        # via the db_types compile hook. Nullable — the web container may not
        # always forward a trustworthy client IP.
        sa.Column("source_ip", postgresql.INET(), nullable=True),
        sa.Column(
            "request_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        # NOTE: deliberately NO `ENABLE ROW LEVEL SECURITY` / `FORCE ROW LEVEL
        # SECURITY` / `CREATE POLICY tenant_isolation` here. This is a
        # control-plane table swept across all tenants by the reaper; see the
        # module docstring for the full RLS-exemption rationale. Do not "fix"
        # this by adding a tenant_isolation policy — it would blind the reaper.
    )


def downgrade() -> None:
    op.drop_table("ephemeral_demo_tenants")
