"""0175_supplier_rules — Document Inbox supplier rules (spec issue #33, phase 2).

Deterministic vendor → coding suggestions (no machine learning): a rule
maps a normalised vendor key (and optionally an ABN) to a contact plus
default account / tax-code / record-kind. Matching runs at extraction
time — ABN-exact first, then vendor_key-exact, first match wins — and is
**suggestion-only** (the bank-rules posture): it fills the four
``suggested_*``/``supplier_rule_id`` columns added to ``inbox_documents``
here, never publishes anything.

Design notes (spec §2 migration B / §6):

* The uniqueness rule is a **hand-written partial expression index**
  (alembic autogenerate will not emit it):
  ``UNIQUE (tenant_id, coalesce(company_id, <nil-uuid>), vendor_key)
  WHERE active`` — one live rule per vendor per (tenant, company) scope,
  with NULL company (tenant-wide) folding onto the nil UUID. The spec
  wrote ``uuid_nil()``, which lives in the uuid-ossp extension; the
  literal ``'00000000-…'::uuid`` is identical and needs no extension.
* Statuses/kinds are TEXT + CHECK, UPPERCASE — never a Postgres enum.
* ``times_applied`` / ``times_overridden`` are the rule-quality signal,
  maintained at publish time (a confirmed application vs a diverging
  publish).
* The four suggestion columns land on ``inbox_documents`` in THIS
  migration (not 0174) to avoid a forward FK to ``supplier_rules``.

RLS checklist (non-negotiable, same commit as the probe test
``tests/test_rls_supplier_rules.py``): tenant_id UUID NOT NULL FK,
ENABLE + FORCE ROW LEVEL SECURITY, ``tenant_isolation`` policy in the
one-policy-shape-for-the-whole-DB form, explicit app-layer tenant
filters, cross-tenant probe test. Explicit GRANT to ``saebooks_app``
(0158 precedent). Tenant-coherence trigger is the table-owned
NULL-tolerant pattern from 0174 (company_id is nullable by design —
NULL means the rule is tenant-wide).

Revision ID: 0175_supplier_rules
Revises: 0174_inbox_documents
Create Date: 2026-07-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0175_supplier_rules"
down_revision: str | None = "0174_inbox_documents"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150/0158).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_TABLE = "supplier_rules"
_DOCS = "inbox_documents"

# uuid_nil() without the uuid-ossp extension dependency.
_NIL_UUID = "'00000000-0000-0000-0000-000000000000'::uuid"


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
    # NULL-tolerant variant of 0131's assert_child_tenant_matches_company()
    # (same pattern as 0174): supplier_rules.company_id is nullable (NULL =
    # tenant-wide rule), so NULL passes; when SET, the company's tenant must
    # equal the row's tenant_id.
    op.execute(
        sa.text(
            f"""CREATE OR REPLACE FUNCTION {table}_tenant_coherence()
            RETURNS trigger AS $$
            DECLARE
                v_company_tenant_id uuid;
            BEGIN
                IF NEW.company_id IS NULL THEN
                    RETURN NEW;
                END IF;

                SELECT tenant_id INTO v_company_tenant_id
                FROM companies
                WHERE id = NEW.company_id;

                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'tenant_coherence: company % not found (table %)',
                        NEW.company_id, TG_TABLE_NAME;
                END IF;

                IF NEW.tenant_id IS DISTINCT FROM v_company_tenant_id THEN
                    RAISE EXCEPTION
                        'tenant_coherence: %.tenant_id (%) must equal companies.tenant_id (%) for company %',
                        TG_TABLE_NAME, NEW.tenant_id, v_company_tenant_id, NEW.company_id;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql"""
        )
    )
    trg = f"trg_{table}_tenant_coherence"
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trg} ON {table}"))
    op.execute(
        sa.text(
            f"CREATE TRIGGER {trg} "
            f"BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {table}_tenant_coherence()"
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
        # NULL = tenant-wide rule; SET = scoped to one company. A rule
        # scoped to a deleted company is garbage → CASCADE.
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=True,
        ),
        # Normalised (lower/trimmed/whitespace-collapsed) vendor name —
        # the service owns the normalisation (normalise_vendor_key).
        sa.Column("vendor_key", sa.String(255), nullable=False),
        # 11-digit Australian Business Number, digits only (normalised).
        sa.Column("vendor_abn", sa.String(11), nullable=True),
        # A rule without its contact is meaningless → CASCADE with the
        # contact hard-delete.
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "tax_code_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tax_codes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("record_kind", sa.Text(), nullable=True),
        sa.Column(
            "origin", sa.Text(), nullable=False, server_default="MANUAL"
        ),
        # Rule-quality signal, maintained at publish time.
        sa.Column(
            "times_applied", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "times_overridden", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_applied_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_from_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inbox_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Soft-delete: active=false retires the rule (frees the unique).
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.text("true")
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
        # TEXT + CHECK, UPPERCASE — never Postgres enums.
        sa.CheckConstraint(
            "record_kind IN ('EXPENSE','BILL','CREDIT_NOTE')",
            name="ck_supplier_rules_record_kind",
        ),
        sa.CheckConstraint(
            "origin IN ('MANUAL','LEARNED')",
            name="ck_supplier_rules_origin",
        ),
        sa.CheckConstraint(
            "vendor_key <> ''",
            name="ck_supplier_rules_vendor_key_nonempty",
        ),
        sa.CheckConstraint(
            "vendor_abn IS NULL OR vendor_abn ~ '^[0-9]{11}$'",
            name="ck_supplier_rules_vendor_abn_digits",
        ),
    )

    # One live rule per vendor per (tenant, company) scope — hand-written
    # partial expression unique (autogenerate will NOT emit this).
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_supplier_rules_scope_vendor "
            f"ON {_TABLE} (tenant_id, coalesce(company_id, {_NIL_UUID}), vendor_key) "
            "WHERE active"
        )
    )
    # Match scans (extraction time): ABN-exact, then vendor_key-exact.
    op.execute(
        sa.text(
            "CREATE INDEX ix_supplier_rules_tenant_abn "
            f"ON {_TABLE} (tenant_id, vendor_abn) "
            "WHERE active AND vendor_abn IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_supplier_rules_tenant_vendor_key "
            f"ON {_TABLE} (tenant_id, vendor_key) WHERE active"
        )
    )

    _apply_rls(_TABLE)
    _apply_coherence_trigger(_TABLE)
    _grant_app(_TABLE)

    # --- inbox_documents: the four suggestion columns (spec §2 mig B) ---
    op.add_column(
        _DOCS,
        sa.Column(
            "suggested_contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        _DOCS,
        sa.Column(
            "suggested_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        _DOCS,
        sa.Column(
            "suggested_tax_code_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tax_codes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        _DOCS,
        sa.Column(
            "supplier_rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supplier_rules.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(_DOCS, "supplier_rule_id")
    op.drop_column(_DOCS, "suggested_tax_code_id")
    op.drop_column(_DOCS, "suggested_account_id")
    op.drop_column(_DOCS, "suggested_contact_id")

    op.execute(
        sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_tenant_coherence ON {_TABLE}")
    )
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.drop_table(_TABLE)
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_TABLE}_tenant_coherence()"))
