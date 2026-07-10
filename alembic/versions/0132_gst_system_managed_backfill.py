"""GST control accounts — backfill system_managed flag with hyphenated codes.

Migration 0009 issued:

    UPDATE accounts SET system_managed = true
    WHERE code IN ('21310', '21330', '21320')

but the live ``accounts.code`` column stores hyphenated codes
(``'2-1310'``, ``'2-1320'``, ``'2-1330'``) per the 0010-era normalisation.
The 0009 WHERE clause matched zero rows on every stack since.

Impact: edits to the GST control accounts skip the audit-snapshot path
in ``services/accounts.py`` (which keys off ``system_managed``). Auto-
posting is unaffected — the ``gst_*_account_code`` settings are literal
strings and were written correctly by 0009.

This migration re-runs the intended UPDATE with the correct hyphenated
codes. Idempotent — manual data-fix already applied 2026-05-24 across
the five live stacks (primary/acme/app-preview/sandbox/cashbook-demo,
21 rows total). For those DBs this is a no-op; for any fresh-seed stack
it does the work 0009 should have done.
"""
from collections.abc import Sequence

from alembic import op


revision: str = "0132_gst_system_managed_backfill"
down_revision: str | None = "0131_tenant_id_coherence_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE accounts SET system_managed = true "
        "WHERE code IN ('2-1310', '2-1320', '2-1330') "
        "  AND system_managed = false"
    )


def downgrade() -> None:
    # Intentional no-op. Reverting system_managed to false would unset
    # an invariant the engine relies on for audit-snapshot semantics on
    # the GST control accounts. If you genuinely need to undo, do it by
    # hand with full awareness.
    pass
