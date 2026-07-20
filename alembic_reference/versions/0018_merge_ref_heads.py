"""0017_merge_ref_heads — join the SubJuris reference chain with the EE merge.

0016_merge_ref_heads (EE night chains) and 0016_subjuris_fk_promotion
(SubJuris slice) both advanced off 0015_duty_domain_gaps in parallel,
leaving two reference heads that break the harness's
``alembic -c alembic_reference.ini upgrade head``. Both chains are
additive with no ordering dependency; no-op merge so ``upgrade head``
resolves.

Revision ID: 0017_merge_ref_heads
Revises: 0016_merge_ref_heads, 0016_subjuris_fk_promotion
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0017_merge_ref_heads"
down_revision: tuple[str, str] = (
    "0016_merge_ref_heads",
    "0016_subjuris_fk_promotion",
)
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
