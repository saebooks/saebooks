"""Surcharge-duty breakdown columns on dutiable_transaction_events
(M1.5 · Wave 5-DUTIES).

Purely additive, following the 0198 pattern (nullable columns, no
default, existing rows stay NULL; validation happens at the service
layer against the reference DB, so there is deliberately NO foreign key
here — the reference DB is a separate database):

* ``dutiable_transaction_events.surcharge_duty`` — the foreign /
  non-resident purchaser surcharge component INCLUDED in
  ``computed_duty`` (informational breakdown; the linked JE still posts
  ``computed_duty`` as one amount, so posting behaviour is unchanged).
* ``dutiable_transaction_events.applied_surcharge_rate_id`` — opaque
  pointer at the reference-DB ``duty_surcharge_rates`` row the surcharge
  was computed from (reference migration 0014), same non-FK posture as
  ``applied_concession_id``.

No RLS change — new columns on an existing tenant-scoped table (RLS
ENABLE+FORCE since 0181), not a new table. Reversible: downgrade drops
the two columns.

Revision ID: 0199_dutiable_event_surcharge
Revises: 0198_coa_statutory_columns
Create Date: 2026-07-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0199_dutiable_event_surcharge"
down_revision: str | None = "0198_coa_statutory_columns"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dutiable_transaction_events",
        sa.Column("surcharge_duty", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "dutiable_transaction_events",
        sa.Column(
            "applied_surcharge_rate_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dutiable_transaction_events", "applied_surcharge_rate_id")
    op.drop_column("dutiable_transaction_events", "surcharge_duty")
