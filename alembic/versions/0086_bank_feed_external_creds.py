"""Add ``bank_feed_external_creds`` for the relay-client side state.

Cat-C / W4 introduces ``RemoteBankFeedsService`` — saebooks-api stops
talking to SISS directly and instead calls the
``feeds.saebooks.com.au`` relay. The relay carries the canonical
connection state (per ``saebooks-feeds-server-contract.md``) but
saebooks-api still needs a thin local mirror so we can:

* show "Connect bank account" / "Sync now" UI without round-tripping
  the relay on every page render,
* anchor a stable per-tenant ``account_id`` (FK to chart-of-accounts)
  for the inserted txns,
* persist ``last_sync_cursor`` for resumable sync without trusting the
  relay round-trip every time, and
* mark a connection ``revoked`` locally even if the relay is down at
  the moment of revocation.

This is a Class-A RLS table per the convention locked in 0055/0083 —
direct ``tenant_id UUID`` column, ``ENABLE`` + ``FORCE`` RLS, single
``tenant_isolation`` policy whose predicate is byte-identical to every
other Class-A table in the DB.

Why a new table — why not extend ``bank_feed_clients``
------------------------------------------------------
``bank_feed_clients`` (mig 0029) is the SISS-direct integration's local
state — it carries ``sds_client_id`` (the SISS-issued client guid) and
is shaped around the SISS / CDR data model (1 row per company, then
``bank_feed_accounts`` underneath it). The relay model is different:
1 row per **connection** (per consent flow), no inner account-list
because the relay holds that. The two table shapes don't fit cleanly
on the same row, and reshaping ``bank_feed_clients`` would break the
legacy SISS-direct router which still needs to load.

So we add a sibling table. The legacy stack continues to work; the
new ``api/v1/bank_feeds`` router writes here. Whichever stack is in
use, RLS is enforced.

Why ``account_id`` is nullable
------------------------------
At consent-initiate time the user may not yet have picked which
chart-of-accounts row the future txns post into — that decision can
arrive later from the "Map" UI. Allowing NULL on creation lets the row
land at consent-initiate; the link can be filled in by a subsequent
PATCH without a second migration.

Why no FK to ``chart_of_accounts``
----------------------------------
The CoA table is named ``accounts`` in this codebase (see ``models/
account.py``). We could install ``FK -> accounts(id)`` here, but doing
so would make this migration depend on the row existing in any test
fixture that doesn't seed an account. Pragmatic choice: keep the
column free-form UUID with an index, validate in the application
layer. This is consistent with how the existing
``bank_feed_accounts.ledger_account_id`` column is shaped — it has the
same FK choice (none, by intent — see mig 0029).

Reversibility
-------------
``downgrade()`` is symmetric: drop policy, NO FORCE, DISABLE RLS, drop
the table. Idempotent (``DROP POLICY IF EXISTS``, ``DROP TABLE IF
EXISTS``).

Revision ID: 0086_bank_feed_external_creds
Revises: 0085_rls_remaining_gaps
Create Date: 2026-05-04
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0086_bank_feed_external_creds"
down_revision: str | None = "0085_rls_remaining_gaps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "bank_feed_external_creds"

# Class-A predicate — byte-identical to migrations 0055, 0083, 0085.
# One predicate definition for the whole DB; copy the shape, never
# invent a new one.
_TENANT_PRED = (
    "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
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
            # Logical FK to chart-of-accounts (table name: ``accounts``).
            # Nullable because the user may map an account after consent
            # initiation (see module docstring). No DB-level FK by intent
            # — same shape as bank_feed_accounts.ledger_account_id.
            "account_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            # SISS-side / relay-side client identifier. The contract
            # exposes this as ``connection_id`` from the relay's POV;
            # we keep the historical column name ``siss_client_id``
            # because ops staff already grep for it in audit trails
            # and the field carries the same semantic value
            # (an opaque token issued by the upstream).
            "siss_client_id",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "last_sync_cursor",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            # Status enum. Stored as TEXT (not a Postgres ENUM) for the
            # same reason saebooks does throughout: app-driven enums
            # are easier to extend without an ALTER TYPE migration.
            # Values: 'pending_consent' | 'active' | 'revoked' | 'error'.
            "status",
            sa.Text(),
            nullable=False,
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
        f"{_TABLE}_tenant_idx",
        _TABLE,
        ["tenant_id"],
    )

    # ENABLE + FORCE RLS, install tenant_isolation. Idempotent —
    # DROP POLICY IF EXISTS ahead of CREATE matches the discipline
    # in 0055 / 0083 / 0085 so a re-run after a partial failure is safe.
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))  # noqa: S608
    op.execute(sa.text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))  # noqa: S608
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {_TABLE} "
            f"FOR ALL USING {_TENANT_PRED} WITH CHECK {_TENANT_PRED}"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))  # noqa: S608
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY"))  # noqa: S608
    op.drop_index(f"{_TABLE}_tenant_idx", table_name=_TABLE)
    op.drop_table(_TABLE)
