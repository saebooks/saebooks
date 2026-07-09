"""tenant-coherence trigger on journal_line_tax_components (M1.5 hardening).

The critic-loop flagged that journal_line_tax_components (added in 0180)
is a company_id-NOT-NULL, FORCE-RLS child table but — unlike its sibling
tenant tables business_identifiers (0147) and bank_routing_identifiers
(0178) — never got the shared tenant-coherence trigger. RLS already
enforces tenant isolation via the tenant_isolation policy; this adds the
belt-and-suspenders check that a row's tenant_id matches its company's
tenant_id (blocks a direct-SQL insert of a foreign company_id), for
consistency with every other tenant-scoped table. Reuses the shared
``assert_child_tenant_matches_company()`` function from 0131.

Revision ID: 0183_jltc_coherence_trigger
Revises: 0182_dutiable_transaction_events
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0183_jltc_coherence_trigger"
down_revision: str | None = "0182_dutiable_transaction_events"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "journal_line_tax_components"
_COHERENCE_FN = "assert_child_tenant_matches_company"
_TRG = f"trg_{_TABLE}_tenant_coherence"


def upgrade() -> None:
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRG} ON {_TABLE}"))
    op.execute(
        sa.text(
            f"CREATE TRIGGER {_TRG} "
            f"BEFORE INSERT OR UPDATE ON {_TABLE} "
            f"FOR EACH ROW EXECUTE FUNCTION {_COHERENCE_FN}()"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRG} ON {_TABLE}"))
