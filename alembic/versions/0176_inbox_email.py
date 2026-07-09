"""0176_inbox_email — Document Inbox email-in tables (spec issue #33, phase 3).

Two tables plus the cross-tenant enumerators for the poller/sweep CLI:

* ``inbox_email_addresses`` — per-tenant ingestion addresses
  ``<token>@in.saebooks.com.au``. The token is server-minted (12+ chars
  lowercase base32, unguessable — the address IS the credential) and
  **globally UNIQUE** via a plain unique constraint: plain uniques fire
  regardless of Row Level Security visibility, which is exactly what the
  routing key needs. **Multiple active addresses per tenant** (one per
  company via ``company_id``) — Richard's own tenant is multi-entity.

* ``inbox_email_messages`` — the per-message processing ledger
  (``UNIQUE (tenant_id, mailbox, message_id)``): what arrived, when it
  was processed, how many documents it produced, how many attachments
  were skipped. Inserted LAST in the poller walk (attachments first,
  ledger row last) so a crash mid-message replays cleanly — completed
  attachments hit the ``inbox_documents`` ``source_ref`` unique and
  skip; no silent loss.

* SECURITY DEFINER enumerators, modelled on 0084's
  ``bank_feeds_active_accounts_for_sync()`` (same GRANT/ownership
  posture: OWNER saebooks, REVOKE PUBLIC, GRANT EXECUTE saebooks_app):

  - ``inbox_email_addresses_for_poll()`` → ``(token, tenant_id,
    company_id)`` for every ACTIVE address — the NOBYPASSRLS poller role
    cannot otherwise build its cross-tenant routing map.
  - ``inbox_documents_tenants_for_sweep()`` → tenant ids with claimable
    or reclaimable inbox documents — the sweep CLI walks per tenant
    under ``SET LOCAL app.current_tenant`` (sync-feeds pattern) and
    needs to discover which tenants to iterate. Addition beyond the
    spec's single named function; same rationale, same posture.

RLS checklist (non-negotiable, same commit as the probe test
``tests/test_rls_inbox_email.py``): tenant_id UUID NOT NULL FK,
ENABLE + FORCE ROW LEVEL SECURITY, ``tenant_isolation`` policy in the
one-policy-shape-for-the-whole-DB form (0055/0088/0150/0158), explicit
app-layer tenant filters, cross-tenant probe test. Explicit GRANT to
``saebooks_app`` (0158 precedent). Statuses/flags are plain columns —
no Postgres enums anywhere. ``inbox_email_addresses`` gets the
table-owned NULL-tolerant tenant-coherence trigger (0174/0175 pattern);
``inbox_email_messages`` has no company column, so no trigger.

Revision ID: 0176_inbox_email
Revises: 0175_supplier_rules
Create Date: 2026-07-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0176_inbox_email"
down_revision: str | None = "0175_supplier_rules"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Reuse the one-policy-shape-for-the-whole-DB predicate (0055/0088/0150/0158).
_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_ADDR = "inbox_email_addresses"
_MSGS = "inbox_email_messages"


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
    # (same pattern as 0174/0175): company_id is nullable (NULL = address
    # routes to the tenant unrouted), so NULL passes; when SET, the
    # company's tenant must equal the row's tenant_id.
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


# --------------------------------------------------------------------------- #
# SECURITY DEFINER enumerators (0084 posture)                                  #
# --------------------------------------------------------------------------- #

_CREATE_POLL_FN = """
CREATE OR REPLACE FUNCTION inbox_email_addresses_for_poll()
RETURNS TABLE (
    token      VARCHAR,
    tenant_id  UUID,
    company_id UUID
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT a.token,
           a.tenant_id,
           a.company_id
    FROM inbox_email_addresses a
    WHERE a.active;
$$;
"""

# VOLATILE (not STABLE): the predicate reads now(), and the sweep wants a
# fresh evaluation every call rather than permitting the planner to reuse
# a snapshot within a surrounding statement.
_CREATE_SWEEP_FN = """
CREATE OR REPLACE FUNCTION inbox_documents_tenants_for_sweep()
RETURNS TABLE (
    tenant_id UUID
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT DISTINCT d.tenant_id
    FROM inbox_documents d
    WHERE (d.status = 'RECEIVED' AND d.next_attempt_at <= now())
       OR (d.status = 'EXTRACTING'
           AND d.claimed_at IS NOT NULL
           AND d.claimed_at < now() - interval '10 minutes');
$$;
"""


def _harden_fn(fn: str) -> None:
    """0084 hardening posture: owner = BYPASSRLS role, deny PUBLIC,
    allow only the runtime app role. Tolerates saebooks_app being
    absent in unusual dev environments (NOTICE, no GRANT)."""
    op.execute(
        sa.text(
            f"""
            DO $do$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks')
                THEN
                    ALTER FUNCTION {fn}() OWNER TO saebooks;
                END IF;
                REVOKE ALL ON FUNCTION {fn}() FROM PUBLIC;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app')
                THEN
                    GRANT EXECUTE ON FUNCTION {fn}() TO saebooks_app;
                ELSE
                    RAISE NOTICE
                        'saebooks_app role not found — skipping GRANT EXECUTE on {fn}(); '
                        'the inbox poll/extract CLI will fail until the role exists '
                        'and the GRANT is issued.';
                END IF;
            END
            $do$;
            """
        )
    )


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # inbox_email_addresses                                              #
    # ------------------------------------------------------------------ #
    op.create_table(
        _ADDR,
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
        # NULL = documents arrive unrouted; SET = default company routing
        # for this address. The token stays a valid tenant credential if
        # its company is deleted → SET NULL, not CASCADE.
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # The routing key AND the credential: server-minted lowercase
        # base32 (RFC 4648 alphabet, lowercased → [a-z2-7]), 12+ chars.
        # Globally unique via a PLAIN unique constraint — uniqueness must
        # hold across tenants regardless of RLS visibility.
        sa.Column("token", sa.String(20), nullable=False, unique=True),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "token ~ '^[a-z2-7]{12,20}$'",
            name="ck_inbox_email_addresses_token_shape",
        ),
    )
    # The tenant's address list (settings page).
    op.execute(
        sa.text(
            f"CREATE INDEX ix_inbox_email_addresses_tenant ON {_ADDR} (tenant_id)"
        )
    )

    _apply_rls(_ADDR)
    _apply_coherence_trigger(_ADDR)
    _grant_app(_ADDR)

    # ------------------------------------------------------------------ #
    # inbox_email_messages                                               #
    # ------------------------------------------------------------------ #
    op.create_table(
        _MSGS,
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
        # Which catch-all mailbox the poller drained (IMAP username /
        # Graph mailbox UPN) — the replay guard is per mailbox.
        sa.Column("mailbox", sa.Text(), nullable=False),
        # RFC 5322 Message-ID.
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("from_addr", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "processed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Inbox-document rows this message accounts for (ingested +
        # byte-duplicate rows + replay-skipped rows from a crashed run).
        sa.Column(
            "document_count", sa.Integer(), nullable=False, server_default="0"
        ),
        # Oversize / wrong-MIME attachments — counted, never ingested
        # (no document row exists without a blob).
        sa.Column(
            "skipped_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "mailbox",
            "message_id",
            name="uq_inbox_email_messages_msg",
        ),
    )

    _apply_rls(_MSGS)
    _grant_app(_MSGS)

    # ------------------------------------------------------------------ #
    # SECURITY DEFINER enumerators                                       #
    # ------------------------------------------------------------------ #
    op.execute(sa.text(_CREATE_POLL_FN))
    _harden_fn("inbox_email_addresses_for_poll")
    op.execute(sa.text(_CREATE_SWEEP_FN))
    _harden_fn("inbox_documents_tenants_for_sweep")


def downgrade() -> None:
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS inbox_documents_tenants_for_sweep();")
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS inbox_email_addresses_for_poll();")
    )
    for table in (_MSGS, _ADDR):
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS trg_{table}_tenant_coherence ON {table}"
            )
        )
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        op.drop_table(table)
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS inbox_email_addresses_tenant_coherence()")
    )
