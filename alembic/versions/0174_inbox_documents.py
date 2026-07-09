"""0174_inbox_documents — Document Inbox capture table (spec issue #33, phase 0).

The inbox and the work queue for the Document Inbox module: photographed
receipts, uploaded documents and (phase 3) emailed supplier invoices land
here as tenant-scoped rows pointing at an unlinked saebooks-vault blob
(``vault_file_id`` — the engine stores no bytes), progress through the
extraction state machine, and are published as DRAFT expenses / bills /
credit notes with full provenance.

Design notes (spec §2, migration A):

* Statuses are TEXT + CHECK, UPPERCASE — never a Postgres enum (the
  SyncProvider-as-TEXT precedent).
* ``extract`` is the verbatim model output and is IMMUTABLE once written;
  reviewer edits live in ``extraction_override``.
* Sweep machinery columns (``attempt_count`` / ``next_attempt_at`` /
  ``claimed_at`` / ``last_error``) are in the schema from day one even
  though the cron sweep is phase 3.
* Rule-suggestion columns (suggested_contact_id / account_id /
  tax_code_id, supplier_rule_id) are deliberately NOT here — they arrive
  with ``supplier_rules`` in migration B (phase 2), avoiding a forward FK.

Indexes:

* ``(tenant_id, status, created_at DESC)`` — the inbox list.
* Partial UNIQUE ``(tenant_id, sha256) WHERE status NOT IN
  ('REJECTED','DUPLICATE')`` — the dedupe backbone at the database level
  (closes the concurrent double-tap race) while keeping a mistaken reject
  recoverable by re-upload.
* Partial UNIQUE ``(tenant_id, source, source_ref) WHERE source_ref IS
  NOT NULL`` — email per-attachment replay guard.
* Partial ``(next_attempt_at) WHERE status IN ('RECEIVED','EXTRACTING')``
  — the phase-3 sweep's claim scan.

RLS checklist (non-negotiable, same commit as the probe test
``tests/test_rls_inbox_documents.py``): tenant_id UUID NOT NULL FK,
ENABLE + FORCE ROW LEVEL SECURITY, ``tenant_isolation`` policy in the
one-policy-shape-for-the-whole-DB form (0055/0088/0150/0158), explicit
app-layer tenant filters, cross-tenant probe test. Explicit GRANT to
``saebooks_app`` because ALTER DEFAULT PRIVILEGES silently misses tables
created by a different role (0158 precedent). Tenant-coherence trigger is
a table-owned NULL-tolerant variant of the 0131
``assert_child_tenant_matches_company()`` function — 0131's raises on
NULL company_id, but inbox_documents.company_id is nullable by design
(assigned at routing/review time, required only at publish).

Revision ID: 0174_inbox_documents
Revises: 0173_capture_schema
Create Date: 2026-07-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0174_inbox_documents"
down_revision: str | None = "0173_capture_schema"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150/0158).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_TABLE = "inbox_documents"

_STATUSES = (
    "'RECEIVED','EXTRACTING','NEEDS_REVIEW','READY','FAILED',"
    "'PUBLISHED','REJECTED','DUPLICATE'"
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


def _apply_coherence_trigger(table: str) -> None:
    # NULL-tolerant variant of 0131's assert_child_tenant_matches_company():
    # inbox_documents.company_id is nullable (assigned at routing/review,
    # required only at publish), so NULL passes through; when SET, the
    # company's tenant must equal the row's tenant_id. Owned by this
    # migration — 0131's function stays untouched.
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
        # NULL until routed/assigned; required at publish (service-enforced).
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # The unlinked vault blob — the engine stores no bytes
        # (attachments.py invariant). No FK: the vault is a separate service.
        sa.Column("vault_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        # EMAIL: '<rfc5322-message-id>#<attachment-index>'
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default="RECEIVED"
        ),
        # Verbatim model output (services/ai_extraction.py) — IMMUTABLE
        # once written. Reviewer edits go in extraction_override only.
        sa.Column("extract", postgresql.JSONB(), nullable=True),
        sa.Column("extraction_override", postgresql.JSONB(), nullable=True),
        sa.Column("extract_model", sa.String(80), nullable=True),
        sa.Column("extraction_confidence", sa.Text(), nullable=True),
        sa.Column("extraction_error", sa.Text(), nullable=True),
        sa.Column("extracted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # Sweep machinery — in schema from day one (used from phase 3).
        sa.Column(
            "attempt_count", sa.SmallInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "duplicate_of_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inbox_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("published_record_kind", sa.Text(), nullable=True),
        # No FK — polymorphic across expenses/bills/credit_notes.
        sa.Column("published_record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "published_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("reject_note", sa.Text(), nullable=True),
        # Optimistic lock (bank_statement_lines precedent).
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_by",
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
        # Statuses: TEXT + CHECK, UPPERCASE — never Postgres enums.
        sa.CheckConstraint(
            "source IN ('UPLOAD','EMAIL','API')",
            name="ck_inbox_documents_source",
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUSES})",
            name="ck_inbox_documents_status",
        ),
        sa.CheckConstraint(
            "extraction_confidence IN ('OK','PARTIAL')",
            name="ck_inbox_documents_extraction_confidence",
        ),
        sa.CheckConstraint(
            "published_record_kind IN ('EXPENSE','BILL','CREDIT_NOTE')",
            name="ck_inbox_documents_published_record_kind",
        ),
        sa.CheckConstraint(
            "reject_reason IN ('DUPLICATE','NOT_A_DOCUMENT','PERSONAL','OTHER')",
            name="ck_inbox_documents_reject_reason",
        ),
    )

    # The inbox list: newest first within a tenant, filtered by status.
    op.execute(
        sa.text(
            "CREATE INDEX ix_inbox_documents_tenant_status_created "
            f"ON {_TABLE} (tenant_id, status, created_at DESC)"
        )
    )
    # Dedupe backbone — excludes terminal-negative statuses so a mistaken
    # reject is recoverable by re-upload.
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_inbox_documents_tenant_sha256 "
            f"ON {_TABLE} (tenant_id, sha256) "
            "WHERE status NOT IN ('REJECTED','DUPLICATE')"
        )
    )
    # Email per-attachment replay guard.
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_inbox_documents_tenant_source_ref "
            f"ON {_TABLE} (tenant_id, source, source_ref) "
            "WHERE source_ref IS NOT NULL"
        )
    )
    # Phase-3 sweep claim scan.
    op.execute(
        sa.text(
            "CREATE INDEX ix_inbox_documents_next_attempt "
            f"ON {_TABLE} (next_attempt_at) "
            "WHERE status IN ('RECEIVED','EXTRACTING')"
        )
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
    # NOTE: do NOT drop assert_child_tenant_matches_company() — owned by 0131.
