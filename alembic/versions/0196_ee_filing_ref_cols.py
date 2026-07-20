"""tax_returns EE X-Road filing ref columns (M3, Option A).

The EE (Estonia) KMD3 filing rail is an async ``submit → poll(UUID) → confirm``
state machine over X-Road (``services/lodgement/adapters/ee_client.py``). That
lifecycle forces the ``feedbackReportId`` UUID and the filing state to persist
BETWEEN calls. Per the build plan Module 3 §3.4 this is done with **Option A** —
additive nullable ref columns on the existing ``tax_returns`` row, not a new
tracking table:

- ``ee_filing_request_id``  — the X-Road ``feedbackReportId`` UUID (poll handle).
- ``ee_filing_state``       — the ``EEFilingState`` value
                              (submitted|pending|accepted|rejected|confirmed).
- ``ee_filing_receipt``     — the parsed operationAccepted/Rejected feedback /
                              koondvaade JSON (JSONB).

Because these hang off the already-tenant-scoped ``tax_returns`` table (which
carries its own ``tenant_id`` + RLS), **no new tenant table is introduced and the
RLS checklist does not apply**. All three are nullable with no server_default and
no backfill — populated only for EE returns filed over X-Road, NULL everywhere
else. Fully reversible via ``op.drop_column``.

Chains off the current company-DB single head ``0195_merge_ee_permissions``
(verified via ``alembic heads``). The suite pins ``len(get_heads()) == 1`` — if a
sibling M-packet also lands a migration off 0195, the orchestrator needs a merge
revision joining them.

Revision ID: 0196_ee_filing_ref_cols
Revises:     0195_merge_ee_permissions
Create Date: 2026-07-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0196_ee_filing_ref_cols"
down_revision: str | None = "0195_merge_ee_permissions"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "tax_returns"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "ee_filing_request_id",
            sa.String(length=255),
            nullable=True,
            comment="EE X-Road KMD3 feedbackReportId (UUID) — the poll handle.",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "ee_filing_state",
            sa.String(length=16),
            nullable=True,
            comment=(
                "EE filing lifecycle state — EEFilingState value "
                "(submitted|pending|accepted|rejected|confirmed)."
            ),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "ee_filing_receipt",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "EE X-Road feedback receipt — parsed operationAccepted/Rejected "
                "(vatPayable/overpaidVat, errors) / koondvaade JSON."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "ee_filing_receipt")
    op.drop_column(_TABLE, "ee_filing_state")
    op.drop_column(_TABLE, "ee_filing_request_id")
