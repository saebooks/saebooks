"""Merge orphan ``zzzz_local_merge_m0_branches`` head into payroll chain.

The script directory currently has two alembic heads:

1. ``0115_leave_balances`` — the canonical payroll chain
   (0109_time_entries → 0110_api_tokens → 0111_employees_and_super_funds
   → 0112_pay_run_lines_extension → 0113_payg_tables
   → 0114_stp_submissions → 0115_leave_balances).

2. ``zzzz_local_merge_m0_branches`` — a no-op stub left over from the
   M0 branch merge (down=0104_journal_lines_tax_treatment). Some live
   DBs are still stamped at this revision (notably saebooks_prod,
   restored from a 2026-05-05 snapshot).

Without a merge migration, ``alembic upgrade head`` refuses with
"Multiple head revisions are present". This merge has no schema
effect — it just collapses the two heads into a single ancestor for
all future migrations.

After this is applied, alembic_version has a single row and forward
work chains off 0116_merge_payroll_into_main.

Revision ID: 0116_merge_payroll_into_main
Revises: 0115_leave_balances, zzzz_local_merge_m0_branches
Create Date: 2026-05-22
"""
from collections.abc import Sequence


revision: str = "0116_merge_payroll_into_main"
down_revision: tuple[str, str] | None = (
    "0115_leave_balances",
    "zzzz_local_merge_m0_branches",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge migration — no schema change."""


def downgrade() -> None:
    """Reverse merge — no schema change."""
