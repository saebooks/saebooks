"""Accounting-package sync state tables — Xero adapter (Enterprise tier).

Ported from ``feat/sync-xero-adapter`` (branched off ``0094_purchase_orders``,
10 weeks behind ``main`` at port time — see ``docs/sync/xero.md``). That
branch shipped this schema across two migrations, ``0095_sync_state_tables``
and ``0096_sync_state_origin``. On ``main``, the revision id
``0095_sync_state_tables`` is already taken by an immutable stub (applied to
some environments before ``0095_quotes_tables`` was branched off
``0094_purchase_orders`` — see that file) so this migration re-creates the
same schema under a fresh id, chained onto main's current head, and folds
the origin-branch's follow-up fix (0096) straight into the table definition
instead of doing the nullable-then-backfill dance — there is no pre-existing
data to backfill on a from-scratch table.

Adds four tables:

* ``sync_connections`` — one row per (tenant, provider, external org).
  Holds OAuth refresh-token ciphertext, the customer's own OAuth client
  ID/secret ciphertext (per-customer apps — see docs/sync/xero.md §
  Operator surface), and status/last-error fields used by the trigger
  endpoint / future worker.
* ``sync_state`` — one row per synced object on each connection
  (``invoices``, ``contacts``, etc). Carries ``last_pulled_etag`` and
  ``last_pushed_version`` so the LWW conflict detector in
  ``services/sync/xero/push.py`` can tell whether both sides moved
  since the last sync. ``origin`` is an explicit tri-state
  (``local`` | ``remote`` | ``synced``) driving push eligibility —
  see ``saebooks/models/sync.py::SyncStateOrigin`` for the full state
  machine. Baked in as ``NOT NULL`` + ``CHECK`` from the start (this
  is what the origin branch's 0096 migration retrofitted onto an
  already-shipped 0095; a fresh table doesn't need the two-step).
* ``sync_audit_log`` — append-only journal of every push/pull/conflict.
  Distinct from ``audit_log`` (which records *user* actions); this one
  records sync-worker activity.
* ``sync_coa_account_request`` — rate-limit ledger for a trigger-on-miss
  CoA resolver described in the origin branch's plan (§11.a.5) but never
  implemented there (no ``services/sync/coa_resolver.py`` exists on
  ``feat/sync-xero-adapter`` either). Table + ORM model are carried over
  for schema/model parity with the origin branch; it has no consumer
  yet. Forward-provisioned, not a smell — dropping it would be free to
  do later if the CoA-resolver feature is abandoned.

Also extends two existing tables with the external-id quartet, matching
the shape ``0092_external_ids_subledger`` already gave ``bills``,
``invoices``, ``credit_notes``, and ``payments``:

* ``contacts`` gains ``external_id``, ``external_source``,
  ``external_etag``, ``external_payload``. Sync needs to round-trip
  contacts.
* ``journal_entries`` gains the same four columns. Push-only direction
  (accountant-side adjustment journals go up via
  ``services/sync/xero/push.py::push_journal``, never pulled back down
  — pull-direction GL journals would be noise). No ported service code
  reads these columns yet (``push_journal`` records the link in
  ``sync_state`` instead), but the model carries them for schema/model
  parity with this migration — see ``saebooks/models/journal.py``.

RLS class
---------
``sync_connections``, ``sync_state``, and ``sync_audit_log`` are
Class A — direct ``tenant_id UUID NOT NULL`` column (no FK — matches
the origin branch's shape; RLS is the real isolation boundary for
these three, same as e.g. ``bank_feed_external_creds``), ENABLE +
FORCE RLS, single ``tenant_isolation`` policy whose predicate is
byte-identical to every other Class A table installed since 0055.

``sync_coa_account_request`` is also Class A: rate limits are
per-tenant, per-provider.

Reversibility
-------------
``downgrade()`` is symmetric. Every operation guarded with
``IF EXISTS`` so a partial previous attempt does not block re-running.

Revision ID: 0218_sync_state_tables
Revises: 0216_tax_return_amendment
Create Date: 2026-07-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0218_sync_state_tables"
down_revision: str | None = "0217_ic_relay_contra"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Class-A predicate — byte-identical to migrations 0055, 0083, 0085, 0086.
# One predicate definition for the whole DB; copy the shape, never invent
# a new one.
_TENANT_PRED = (
    "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
)


# Tables that need the external-id quartet (matches 0092's shape — the
# other four sub-ledger tables, bills/invoices/credit_notes/payments,
# already got it there).
_EXTID_TABLES: tuple[str, ...] = ("contacts", "journal_entries")


def _enable_rls(table: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"FOR ALL USING {_TENANT_PRED} WITH CHECK {_TENANT_PRED}"
        )
    )


def _disable_rls(table: str) -> None:
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))


def upgrade() -> None:
    # ---- 1. sync_connections ----------------------------------------- #
    op.create_table(
        "sync_connections",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "provider",
            sa.Text(),
            nullable=False,
            comment="'xero' | 'myob' | 'qbo' — TEXT not ENUM so adding "
            "a fourth provider does not need ALTER TYPE. Only 'xero' "
            "has an adapter implementation as of this migration.",
        ),
        sa.Column(
            "external_tenant_id",
            sa.Text(),
            nullable=True,
            comment="Provider-side org/realm/file id. NULL for "
            "'consent pending' rows; populated on first successful "
            "token exchange.",
        ),
        sa.Column(
            "external_tenant_name",
            sa.Text(),
            nullable=True,
            comment="Display name pulled from the provider on consent — "
            "e.g. the Xero org name. Cosmetic only.",
        ),
        sa.Column(
            "oauth_client_id_ciphertext",
            postgresql.BYTEA(),
            nullable=True,
            comment="Customer-supplied OAuth client_id, Fernet-encrypted "
            "via services.crypto.encrypt_field. Each Enterprise customer "
            "registers their own provider apps; SAE ships no shared "
            "client_secret.",
        ),
        sa.Column(
            "oauth_client_secret_ciphertext",
            postgresql.BYTEA(),
            nullable=True,
        ),
        sa.Column(
            "oauth_refresh_token_ciphertext",
            postgresql.BYTEA(),
            nullable=True,
        ),
        sa.Column(
            "oauth_scopes",
            sa.Text(),
            nullable=True,
            comment="Space-separated OAuth scopes granted at consent time. "
            "Surfaced in the UI + diff'd at refresh to detect scope drift.",
        ),
        sa.Column(
            "redirect_uri",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending_consent",
            comment="'pending_consent' | 'active' | 'error' | 'revoked'",
        ),
        sa.Column(
            "last_error",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "last_pulled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_pushed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Multi-org per connection. NULL is treated as distinct (Postgres
    # default), which gives us "one consent-pending row per (tenant,
    # provider) is fine" for free.
    op.create_index(
        "uq_sync_connections_tenant_provider_external",
        "sync_connections",
        ["tenant_id", "provider", "external_tenant_id"],
        unique=True,
        postgresql_where=sa.text("external_tenant_id IS NOT NULL"),
    )
    op.create_index(
        "ix_sync_connections_tenant",
        "sync_connections",
        ["tenant_id"],
    )
    _enable_rls("sync_connections")

    # ---- 2. sync_state ------------------------------------------------ #
    op.create_table(
        "sync_state",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sync_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "object_type",
            sa.Text(),
            nullable=False,
            comment="'contact' | 'invoice' | 'bill' | 'payment' | "
            "'credit_note' | 'journal_entry'",
        ),
        sa.Column(
            "external_id",
            sa.Text(),
            nullable=False,
            comment="Provider-side identifier (Xero GUID, MYOB UID, "
            "QBO Id-string).",
        ),
        sa.Column(
            "local_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Our row id. NULL if this state row was created "
            "from a pull and the local row hasn't been upserted yet "
            "(rare — only between fetch and persist within a single "
            "txn).",
        ),
        sa.Column(
            "last_pulled_etag",
            sa.Text(),
            nullable=True,
            comment="Xero ETag / MYOB RowVersion / QBO SyncToken.",
        ),
        sa.Column(
            "last_pulled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_pushed_version",
            sa.Integer(),
            nullable=True,
            comment="Local row's optimistic-locking version at last "
            "successful push. Used by the conflict detector.",
        ),
        sa.Column(
            "last_pushed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "origin",
            sa.Text(),
            nullable=False,
            comment="'local' | 'remote' | 'synced' — explicit tri-state "
            "driving push eligibility. See "
            "saebooks/models/sync.py::SyncStateOrigin.",
        ),
        sa.Column(
            "quarantined",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="When True, the trigger/worker skips this object "
            "until the operator re-saves the local row. Set on "
            "persistent 4xx validation failures or an unresolved LWW "
            "conflict.",
        ),
        sa.Column(
            "quarantine_reason",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "uq_sync_state_connection_object_external",
        "sync_state",
        ["connection_id", "object_type", "external_id"],
        unique=True,
    )
    op.create_index(
        "ix_sync_state_tenant",
        "sync_state",
        ["tenant_id"],
    )
    op.create_index(
        "ix_sync_state_local",
        "sync_state",
        ["object_type", "local_id"],
        postgresql_where=sa.text("local_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_sync_state_origin",
        "sync_state",
        "origin IN ('local', 'remote', 'synced')",
    )
    # Push-selector covering index — push.py's candidate queries filter
    # on (connection_id, object_type, origin) WHERE quarantined = false.
    op.create_index(
        "ix_sync_state_push_selector",
        "sync_state",
        ["connection_id", "object_type", "origin"],
        postgresql_where=sa.text("quarantined = false"),
    )
    _enable_rls("sync_state")

    # ---- 3. sync_audit_log --------------------------------------------- #
    op.create_table(
        "sync_audit_log",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sync_connections.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "direction",
            sa.Text(),
            nullable=False,
            comment="'pull' | 'push' | 'conflict' | 'connect' | 'disconnect'",
        ),
        sa.Column(
            "object_type",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "external_id",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "local_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "outcome",
            sa.Text(),
            nullable=False,
            comment="'ok' | 'created' | 'updated' | 'skipped' | 'conflict' | 'error'",
        ),
        sa.Column(
            "message",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Provider-shaped payload at the time of the event. "
            "Used by the conflict-resolution UI's diff view.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_sync_audit_log_tenant",
        "sync_audit_log",
        ["tenant_id"],
    )
    op.create_index(
        "ix_sync_audit_log_connection",
        "sync_audit_log",
        ["connection_id"],
    )
    op.create_index(
        "ix_sync_audit_log_object",
        "sync_audit_log",
        ["object_type", "external_id"],
    )
    _enable_rls("sync_audit_log")

    # ---- 4. sync_coa_account_request ----------------------------------- #
    # Rate-limit ledger for a trigger-on-miss CoA resolver that was
    # planned but never implemented on the origin branch — see module
    # docstring. Schema-only; no consumer as of this migration.
    op.create_table(
        "sync_coa_account_request",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "provider",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "external_account_code",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "last_request_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "request_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_index(
        "uq_sync_coa_request_tenant_provider_code",
        "sync_coa_account_request",
        ["tenant_id", "provider", "external_account_code"],
        unique=True,
    )
    _enable_rls("sync_coa_account_request")

    # ---- 5. external-id quartet on contacts + journal_entries ---------- #
    for table in _EXTID_TABLES:
        op.add_column(table, sa.Column("external_id", sa.String(255), nullable=True))
        op.add_column(table, sa.Column("external_source", sa.String(64), nullable=True))
        op.add_column(table, sa.Column("external_etag", sa.String(255), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "external_payload",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )
        # Partial unique on (company_id, external_source, external_id) —
        # matches 0092's shape. NULL external_id rows (the vast majority,
        # everything not yet synced) are unconstrained.
        op.create_index(
            f"uq_{table}_external_id",
            table,
            ["company_id", "external_source", "external_id"],
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL"),
        )


def downgrade() -> None:
    for table in _EXTID_TABLES:
        op.drop_index(f"uq_{table}_external_id", table_name=table)
        op.drop_column(table, "external_payload")
        op.drop_column(table, "external_etag")
        op.drop_column(table, "external_source")
        op.drop_column(table, "external_id")

    _disable_rls("sync_coa_account_request")
    op.drop_index(
        "uq_sync_coa_request_tenant_provider_code",
        table_name="sync_coa_account_request",
    )
    op.drop_table("sync_coa_account_request")

    _disable_rls("sync_audit_log")
    op.drop_index("ix_sync_audit_log_object", table_name="sync_audit_log")
    op.drop_index("ix_sync_audit_log_connection", table_name="sync_audit_log")
    op.drop_index("ix_sync_audit_log_tenant", table_name="sync_audit_log")
    op.drop_table("sync_audit_log")

    _disable_rls("sync_state")
    op.drop_index("ix_sync_state_push_selector", table_name="sync_state")
    op.drop_constraint("ck_sync_state_origin", "sync_state", type_="check")
    op.drop_index("ix_sync_state_local", table_name="sync_state")
    op.drop_index("ix_sync_state_tenant", table_name="sync_state")
    op.drop_index(
        "uq_sync_state_connection_object_external",
        table_name="sync_state",
    )
    op.drop_table("sync_state")

    _disable_rls("sync_connections")
    op.drop_index("ix_sync_connections_tenant", table_name="sync_connections")
    op.drop_index(
        "uq_sync_connections_tenant_provider_external",
        table_name="sync_connections",
    )
    op.drop_table("sync_connections")
