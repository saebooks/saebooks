"""0178_bank_routing_identifiers — canonical bank-routing identifiers (M1.5 · T10).

Bank routing today is AU-only: fixed ``bsb`` / ``apca_user_id`` columns
scattered across ``accounts``, ``contacts``, ``employees`` and
``super_funds``. This migration adds a new, ADDITIVE table so the engine
can also store an IBAN, a SWIFT/BIC, a US ABA routing number, a UK sort
code, or a SEPA reference — not just an Australian BSB.

The existing ``bsb`` / ``apca_user_id`` / bank columns on those four
tables are left untouched: this table is a jurisdiction-neutral
superset, keyed by ``(company_id, owner_type, owner_id, routing_scheme)``
so one owner can carry more than one routing identifier. ``owner_id`` has
no cross-table FK — the owner table varies with ``owner_type`` and
Postgres FKs cannot target a variable table (see the model docstring,
``saebooks/models/bank_routing_identifier.py``).

RLS checklist (non-negotiable, same commit as the probe test
``tests/services/test_bank_routing_identifiers.py``): tenant_id UUID NOT
NULL FK, ENABLE + FORCE ROW LEVEL SECURITY, ``tenant_isolation`` policy
in the one-policy-shape-for-the-whole-DB form (0055/0088/0150/0158/0175),
a tenant-coherence trigger (reusing the shared
``assert_child_tenant_matches_company()`` function from 0131 — same
posture as 0147's business_identifiers close-out, since ``company_id``
here is NOT NULL like the original eight tables), an explicit GRANT to
``saebooks_app``, and a live cross-tenant probe test.

Revision ID: 0178_bank_routing_identifiers
Revises: 0177_company_entity_structure
Create Date: 2026-07-09
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0178_bank_routing_identifiers"
down_revision: str | None = "0177_company_entity_structure"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150/0158/0175).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_TABLE = "bank_routing_identifiers"
_COHERENCE_FN = "assert_child_tenant_matches_company"


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
    # company_id is NOT NULL on this table (every routing identifier
    # belongs to exactly one company) — reuse the shared, already-present
    # non-null-tolerant function from 0131 rather than writing a new one
    # (same reuse 0147 did to close out business_identifiers).
    trg = f"trg_{table}_tenant_coherence"
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trg} ON {table}"))
    op.execute(
        sa.text(
            f"CREATE TRIGGER {trg} "
            f"BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_COHERENCE_FN}()"
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
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Free-text — validated at the service layer against
        # BankRoutingOwnerType. No cross-table FK: the owner table
        # varies by owner_type.
        sa.Column("owner_type", sa.String(16), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Free-text — validated at the service layer against
        # BankRoutingScheme. Kept as String (not a Postgres enum) so a
        # new scheme is a code-only change, mirroring
        # business_identifiers.scheme.
        sa.Column("routing_scheme", sa.String(32), nullable=False),
        sa.Column(
            "scheme_value",
            sa.String(64),
            nullable=False,
            comment="The routing number/BSB/IBAN/sort code for this scheme.",
        ),
        sa.Column(
            "bic",
            sa.String(11),
            nullable=True,
            comment="Optional SWIFT BIC alongside a national scheme.",
        ),
        sa.Column("account_number", sa.String(64), nullable=True),
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
            "owner_type",
            "owner_id",
            "routing_scheme",
            name="uq_bank_routing_identifiers_owner_scheme",
        ),
    )

    op.create_index(
        "ix_bank_routing_identifiers_company_id",
        _TABLE,
        ["company_id"],
    )
    op.create_index(
        "ix_bank_routing_identifiers_tenant_id",
        _TABLE,
        ["tenant_id"],
    )
    op.create_index(
        "ix_bank_routing_identifiers_owner",
        _TABLE,
        ["owner_type", "owner_id"],
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
    op.drop_index("ix_bank_routing_identifiers_owner", table_name=_TABLE)
    op.drop_index("ix_bank_routing_identifiers_tenant_id", table_name=_TABLE)
    op.drop_index("ix_bank_routing_identifiers_company_id", table_name=_TABLE)
    op.drop_table(_TABLE)
