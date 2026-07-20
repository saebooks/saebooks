"""0016_merge_ref_heads — join the BIK amount-per-unit chain with the duty-domain chain.

feat/ee-product-gaps added 0012_bik_amount_per_unit off 0011_oss_member_state_rates
while the mainline reference chain advanced 0012_reference_rename_sweep…0015_duty_domain_gaps
in parallel. Both additive; no-op merge revision.

Revision ID: 0016_merge_ref_heads
Revises: 0015_duty_domain_gaps, 0012_bik_amount_per_unit
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0016_merge_ref_heads"
down_revision: tuple[str, str] = ("0015_duty_domain_gaps", "0012_bik_amount_per_unit")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
