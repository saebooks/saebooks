"""0139_merge_heads — merge the canonical chain with the local-merge dev branch.

The repo carries a dev-only ``zzzz_local_merge_m0_branches`` revision
that originally rescued a developer DB stuck at a parallel 0102 head.
With 0132a_backfill in place, fresh installs no longer need that
rescue path — but the file is checked in, so alembic sees both
``0138_tpar`` and ``zzzz_local_merge_m0_branches`` as heads.

This is a no-op merge revision that joins them so ``alembic upgrade
head`` works cleanly without --branchname.

Revision ID: 0139_merge_heads
Revises: 0138_tpar, zzzz_local_merge_m0_branches
Create Date: 2026-05-27
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0139_merge_heads"
down_revision: tuple[str, str] = ("0138_tpar", "zzzz_local_merge_m0_branches")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
