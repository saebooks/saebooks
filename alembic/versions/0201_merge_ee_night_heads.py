"""0201_merge_ee_night_heads — join the EE product-gaps chain with the merged mainline.

feat/ee-product-gaps chained 0197_ee_fringe_benefit_cols…0200_ee_payroll_control_accounts
off 0196_ee_filing_ref_cols while the mainline advanced through the e-invoice merge to
0200_merge_einvoice_heads in parallel. Both chains additive, no ordering dependency;
no-op merge so ``alembic upgrade head`` resolves.

Revision ID: 0201_merge_ee_night_heads
Revises: 0200_merge_einvoice_heads, 0200_ee_payroll_control_accounts
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0201_merge_ee_night_heads"
down_revision: tuple[str, str] = ("0200_merge_einvoice_heads", "0200_ee_payroll_control_accounts")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
