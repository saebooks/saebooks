"""company_jurisdictions — company ↔ jurisdiction m2m membership
(M1.5 · 5-SUBJURIS, K5 breadth).

``Company.jurisdiction`` is a single scalar routing key. The K5 audit
flagged the missing breadth half: a company operating across multiple
sub-national jurisdictions (payroll-tax employer in QLD *and* NSW, a US
company with sales-tax nexus in several states) has nowhere to record
that membership. This is that record — one row per (company,
jurisdiction), purely ADDITIVE: nothing on the posting path reads it and
``Company.jurisdiction`` stays the routing key, so AU behaviour is
unchanged by construction.

``jurisdiction_code`` holds reference-DB T3 tree codes ('AUS',
'AU-QLD') as free text, non-FK — the reference DB is a separate
database (same posture as ``Company.jurisdiction``).

One tenant-scoped table following the non-negotiable new-table RLS
checklist (same shape as 0155/0178/0182):

  * ``tenant_id`` NOT NULL + FK ``tenants`` (RESTRICT) and ``company_id``
    NOT NULL + FK ``companies`` (CASCADE).
  * ``ENABLE`` + ``FORCE`` ROW LEVEL SECURITY + the standard
    ``tenant_isolation`` policy (the verbatim 0055/0088/0150/0155/0178
    ``app.current_tenant`` predicate, with a ``WITH CHECK`` clause).
  * The 0131 ``assert_child_tenant_matches_company`` coherence trigger so
    ``tenant_id`` can never disagree with ``companies.tenant_id`` for the
    row's ``company_id``.
  * Explicit ``GRANT … TO saebooks_app`` (default privileges silently miss
    tables created under the non-owner migration role).

Reversible: ``downgrade`` drops the table and its trigger/policy.

Revision ID: 0201_company_jurisdictions
Revises:     0200_merge_einvoice_heads
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0201_company_jurisdictions"
down_revision: str | None = "0200_merge_einvoice_heads"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150/0155/0178).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_TABLE = "company_jurisdictions"


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
        # Reference-DB T3 tree code: 'AUS' (country) or 'AU-QLD'
        # (sub-national). Free text, non-FK — cross-DB (see module
        # docstring).
        sa.Column("jurisdiction_code", sa.String(6), nullable=False),
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
        sa.UniqueConstraint(
            "company_id",
            "jurisdiction_code",
            name="uq_company_jurisdictions_natkey",
        ),
    )
    op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])
    op.create_index(f"ix_{_TABLE}_company_id", _TABLE, ["company_id"])

    _apply_rls(_TABLE)
    _apply_coherence_trigger(_TABLE)
    _grant_app(_TABLE)


def downgrade() -> None:
    # Drop trigger + policy explicitly first (mirrors the 0155/0178/0182
    # downgrade shape and stays idempotent), then the table.
    op.execute(
        sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_tenant_coherence ON {_TABLE}")
    )
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table(_TABLE)
    # NOTE: do NOT drop assert_child_tenant_matches_company() — it is
    # owned by 0131 and many other triggers depend on it.
