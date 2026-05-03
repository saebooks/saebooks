"""bank_feeds: SECURITY DEFINER enumerator for sync-feeds CLI under RLS.

Why this migration exists
-------------------------
Migration 0056 split the DB role into ``saebooks`` (owner, BYPASSRLS) and
``saebooks_app`` (runtime, NOBYPASSRLS). Migration 0055 forces
``tenant_isolation`` RLS on every customer-data table.

The ``python -m saebooks.cli sync-feeds`` cron walks **every** active
``BankFeedAccount`` across **every** tenant in one process. Under RLS
that is a contradiction: a session bound to ``saebooks_app`` can only
see one tenant's rows at a time (the tenant whose UUID is set on
``app.current_tenant``). Naively SELECT-ing from ``bank_feed_accounts``
with no GUC set returns zero rows.

The fix has two parts:

1. The CLI sets ``app.current_tenant`` per company / tenant pair before
   syncing it (see ``saebooks/cli.py``).
2. The CLI needs a way to enumerate "what tenants do I need to iterate"
   *without* itself bypassing RLS.

This migration provides the enumerator: a SECURITY DEFINER function
that the runtime role is allowed to ``EXECUTE``. The function runs as
the owner role (``saebooks``, BYPASSRLS=t) and returns the
``(company_id, tenant_id, account_id)`` triple for every active
bank-feed account regardless of any GUC setting on the calling
session.

Filter
------
The function mirrors ``saebooks/services/bank_feeds/onboarding.py:566``
exactly: ``revoked_at IS NULL`` only. The ``bank_feed_accounts`` table
has neither an ``is_active`` flag nor an ``archived_at`` column — being
unrevoked is what "active" means at the data layer. ``BankFeedClient``
has its own ``active`` flag but the CLI applies that filter in Python
on the way through; the enumerator stays narrow.

Privileges
----------
* ``OWNER TO saebooks`` — owner is the BYPASSRLS role.
* ``REVOKE ALL FROM PUBLIC`` — defence in depth.
* ``GRANT EXECUTE TO saebooks_app`` — the only non-owner caller that
  needs it.

``STABLE`` (vs ``VOLATILE``) so the planner is allowed to inline /
hash the call inside a single statement; the function does no writes
and the result depends only on table contents.

Reversibility
-------------
``DROP FUNCTION IF EXISTS`` — idempotent and safe even if the function
was already removed by hand.

Revision ID: 0084_bank_feeds_secdef_enum
Revises: 0083_close_tenant_rls_gaps
Create Date: 2026-05-03
"""
from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "0084_bank_feeds_secdef_enum"
down_revision: str | None = "0083_close_tenant_rls_gaps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CREATE_FN = """
CREATE OR REPLACE FUNCTION bank_feeds_active_accounts_for_sync()
RETURNS TABLE (
    company_id UUID,
    tenant_id  UUID,
    account_id UUID
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT bfa.company_id,
           c.tenant_id,
           bfa.id AS account_id
    FROM bank_feed_accounts bfa
    JOIN companies c ON c.id = bfa.company_id
    WHERE bfa.revoked_at IS NULL;
$$;
"""

_HARDEN_FN = """
ALTER FUNCTION bank_feeds_active_accounts_for_sync() OWNER TO saebooks;
REVOKE ALL ON FUNCTION bank_feeds_active_accounts_for_sync() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bank_feeds_active_accounts_for_sync() TO saebooks_app;
"""

_DROP_FN = "DROP FUNCTION IF EXISTS bank_feeds_active_accounts_for_sync();"


def upgrade() -> None:
    """Install the SECURITY DEFINER enumerator."""
    op.execute(text(_CREATE_FN))
    # Hardening is split because ALTER OWNER / REVOKE / GRANT must run
    # as separate statements and only after the function exists. We
    # tolerate the saebooks_app role being absent in unusual dev
    # environments — but in any DB created from migration 0056 the
    # role exists and this block runs cleanly.
    op.execute(
        text(
            """
            DO $do$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app')
                THEN
                    ALTER FUNCTION bank_feeds_active_accounts_for_sync()
                        OWNER TO saebooks;
                    REVOKE ALL ON FUNCTION bank_feeds_active_accounts_for_sync()
                        FROM PUBLIC;
                    GRANT EXECUTE ON FUNCTION bank_feeds_active_accounts_for_sync()
                        TO saebooks_app;
                ELSE
                    -- saebooks_app missing: still revoke from PUBLIC so
                    -- nothing bad lands by default. Log a NOTICE so the
                    -- operator sees the gap on alembic upgrade.
                    REVOKE ALL ON FUNCTION bank_feeds_active_accounts_for_sync()
                        FROM PUBLIC;
                    RAISE NOTICE
                        'saebooks_app role not found — skipping GRANT EXECUTE; '
                        'CLI sync-feeds will fail until the role exists and '
                        'this migration is re-run or the GRANT is issued by hand.';
                END IF;
            END
            $do$;
            """
        )
    )


def downgrade() -> None:
    """Remove the enumerator. Idempotent."""
    op.execute(text(_DROP_FN))
