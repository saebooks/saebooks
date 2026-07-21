"""holiday_calendars.calendar_scope (M1.5 P1 tail).

Purely additive. Some jurisdictions' "public holidays" differ between
tax-filing due-date arithmetic and bank-processing/business-day shifts.
NULL = applies to both — unchanged behaviour for every existing row,
since every current consumer treats every holiday as universally
applicable.

Reference-DB only, no RLS concerns (not tenant-scoped). Reversible:
downgrade drops the column.

Revision ID: 0019_holiday_calendar_scope
Revises: 0017_merge_ref_heads
Create Date: 2026-07-15
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_holiday_calendar_scope"
down_revision: str | None = "0017_merge_ref_heads"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "holiday_calendars",
        sa.Column("calendar_scope", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("holiday_calendars", "calendar_scope")
