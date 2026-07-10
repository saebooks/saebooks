"""inventory_cost_layers — perpetual FIFO cost layers (Wave D)

Revision ID: 0186_inventory_cost_layers
Revises:     0185_company_costing_method
Create Date: 2026-07-10

New, ADDITIVE, tenant-scoped table backing the ``fifo`` costing method
(Richard's decision 2 — costing is a per-company setting). One row per
stock RECEIPT for a FIFO company: a receipt creates a layer, an issue
consumes layers oldest-first and posts COGS from the consumed layers via
the existing journal chokepoint (never a manual JE).

Follows the non-negotiable new-table RLS checklist (same shape as
``dutiable_transaction_events`` / migration 0182):

  * ``tenant_id`` NOT NULL + FK ``tenants`` (RESTRICT) and ``company_id``
    NOT NULL + FK ``companies`` (CASCADE).
  * ENABLE + FORCE ROW LEVEL SECURITY + the standard ``tenant_isolation``
    policy (verbatim 0055/0155/0182 ``app.current_tenant`` predicate,
    with a WITH CHECK clause).
  * The 0131 ``assert_child_tenant_matches_company`` coherence trigger so
    ``tenant_id`` can never disagree with ``companies.tenant_id`` for the
    row's ``company_id``.
  * A composite ``(item_id, company_id)`` -> ``items(id, company_id)`` FK
    so the DB refuses a layer pointing at a sister company's item. This
    needs a UNIQUE (id, company_id) on ``items`` — added here (mirrors
    what 0152 did for ``accounts`` / ``uq_accounts_id_company``), guarded
    idempotently.
  * Explicit GRANT ... TO ``saebooks_app`` (default privileges miss
    tables created under the non-owner migration role).

Reversible: ``downgrade`` drops the table, its trigger/policy, and the
items UNIQUE constraint.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0188_inventory_cost_layers"
down_revision: str | None = "0187_company_costing_method"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0155/0182).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_TABLE = "inventory_cost_layers"


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


def _apply_coherence_trigger(table: str) -> None:
    # Reuse the existing 0131 assert_child_tenant_matches_company() function —
    # every row's tenant_id must equal companies.tenant_id for its company_id.
    trg = f"trg_{table}_tenant_coherence"
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trg} ON {table}"))
    op.execute(
        sa.text(
            f"CREATE TRIGGER {trg} "
            f"BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION assert_child_tenant_matches_company()"
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
    # Composite-FK target: UNIQUE (id, company_id) on items (mirrors the
    # 0152 accounts pattern). Idempotent guard so re-runs are safe.
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_items_id_company'
                ) THEN
                    ALTER TABLE items
                        ADD CONSTRAINT uq_items_id_company UNIQUE (id, company_id);
                END IF;
            END $$;
            """
        )
    )

    op.create_table(
        _TABLE,
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
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # item_id is FK'd via the composite constraint below.
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=False),
        sa.Column("original_qty", sa.Numeric(18, 4), nullable=False),
        sa.Column("remaining_qty", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Cross-company guard: item must be an item OF company_id.
        sa.ForeignKeyConstraint(
            ["item_id", "company_id"],
            ["items.id", "items.company_id"],
            name="fk_inventory_cost_layers_item_company",
            ondelete="CASCADE",
        ),
    )
    op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])
    op.create_index(f"ix_{_TABLE}_company_id", _TABLE, ["company_id"])
    # FIFO consumption order + open-layer lookups: (item_id, received_date,
    # id) filtered to layers with stock still available.
    op.create_index(
        f"ix_{_TABLE}_item_open",
        _TABLE,
        ["item_id", "received_date", "id"],
        postgresql_where=sa.text("remaining_qty > 0"),
    )

    _apply_rls(_TABLE)
    _apply_coherence_trigger(_TABLE)
    _grant_app(_TABLE)


def downgrade() -> None:
    op.execute(
        sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_tenant_coherence ON {_TABLE}")
    )
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table(_TABLE)
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_items_id_company'
                ) THEN
                    ALTER TABLE items DROP CONSTRAINT uq_items_id_company;
                END IF;
            END $$;
            """
        )
    )
