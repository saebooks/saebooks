"""0185_scheduled_backups — per-tenant scheduled-backup config + run
tables (planned-modules build-out Wave E, FLAG_SCHEDULED_BACKUPS).

Two new tenant-scoped tables, same tenant-only shape as
``inbox_documents`` (0174) — no ``company_id`` column, so no
tenant-coherence trigger is needed (there is no child FK to a company
row to keep coherent; see ``models/scheduled_backup_config.py``
docstring).

Tenant-scoping checklist (see feedback_new-table-rls-checklist),
applied to BOTH tables:
[x] tenant_id NOT NULL column
[x] FK to tenants(id)
[x] ENABLE ROW LEVEL SECURITY + FORCE ROW LEVEL SECURITY
[x] CREATE POLICY tenant_isolation (USING + WITH CHECK)
[x] Index on (tenant_id, ...)
[x] Service-layer filter as defence-in-depth (services/scheduled_backups.py)
[x] Always-set on writes (service layer stamps tenant_id from the
    request-scoped session, same pattern as every other v1 write path)
[x] Cross-tenant probe test added (tests/test_rls_scheduled_backups.py)
[x] RLS probe test added (same file)

Revision ID: 0185_scheduled_backups
Revises:     0184_jltc_grant_app_role
Create Date: 2026-07-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0189_scheduled_backups"
down_revision: str | None = "0188_inventory_cost_layers"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150/
# 0174/0178/0182).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_CONFIG_TABLE = "scheduled_backup_configs"
_RUN_TABLE = "scheduled_backup_runs"

_DESTINATION_TYPES = "'local_path','rclone_remote'"
_MANAGED_BY_VALUES = "'client','sae'"
_RUN_STATUSES = "'PENDING','RUNNING','SUCCESS','FAILED'"
_REMOTE_PUSH_STATUSES = (
    "'not_applicable','stubbed_not_implemented','pending','success','failed'"
)


def _apply_rls(table: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
        )
    )


def _grant_app(table: str) -> None:
    op.execute(
        sa.text(
            f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO saebooks_app;
                END IF;
            END $$;
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        _CONFIG_TABLE,
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
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "destination_type",
            sa.String(32),
            nullable=False,
            server_default="local_path",
        ),
        sa.Column(
            "destination_params",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "managed_by", sa.String(16), nullable=False, server_default="client"
        ),
        sa.Column("retention_keep_n", sa.Integer(), nullable=True),
        sa.Column("retention_keep_days", sa.Integer(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"destination_type IN ({_DESTINATION_TYPES})",
            name="ck_scheduled_backup_configs_destination_type",
        ),
        sa.CheckConstraint(
            f"managed_by IN ({_MANAGED_BY_VALUES})",
            name="ck_scheduled_backup_configs_managed_by",
        ),
    )
    op.create_index(
        f"ix_{_CONFIG_TABLE}_tenant_id", _CONFIG_TABLE, ["tenant_id"]
    )
    # One backup config per tenant (v1 — a tenant configures ONE
    # destination/retention policy; multiple named configs are a future
    # extension, not needed for Wave E's scope).
    op.create_index(
        f"uq_{_CONFIG_TABLE}_tenant_id",
        _CONFIG_TABLE,
        ["tenant_id"],
        unique=True,
    )

    op.create_table(
        _RUN_TABLE,
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
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "config_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_CONFIG_TABLE}.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="PENDING"
        ),
        sa.Column("destination_type", sa.String(32), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("artifact_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("artifact_sha256", sa.CHAR(64), nullable=True),
        sa.Column("table_counts", postgresql.JSONB(), nullable=True),
        sa.Column(
            "remote_push_status",
            sa.String(32),
            nullable=False,
            server_default="not_applicable",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN ({_RUN_STATUSES})",
            name="ck_scheduled_backup_runs_status",
        ),
        sa.CheckConstraint(
            f"remote_push_status IN ({_REMOTE_PUSH_STATUSES})",
            name="ck_scheduled_backup_runs_remote_push_status",
        ),
    )
    op.create_index(
        f"ix_{_RUN_TABLE}_tenant_created",
        _RUN_TABLE,
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(f"ix_{_RUN_TABLE}_config_id", _RUN_TABLE, ["config_id"])

    _apply_rls(_CONFIG_TABLE)
    _apply_rls(_RUN_TABLE)
    _grant_app(_CONFIG_TABLE)
    _grant_app(_RUN_TABLE)


def downgrade() -> None:
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_RUN_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_RUN_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table(_RUN_TABLE)
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_CONFIG_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_CONFIG_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table(_CONFIG_TABLE)
