"""0200_merge_einvoice_heads — join the e-invoice chain with the dutiable-event chain.

feat/ee-einvoice added 0197_contact_einvoice_recipient off the head as of its
branch point, while the main lane's chain advanced to
0199_dutiable_event_surcharge in parallel. Both chains are additive with no
ordering dependency; this no-op merge revision joins them so
``alembic upgrade head`` resolves without --branchname.

Revision ID: 0200_merge_einvoice_heads
Revises: 0199_dutiable_event_surcharge, 0197_contact_einvoice_recipient
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0200_merge_einvoice_heads"
down_revision: tuple[str, str] = ("0199_dutiable_event_surcharge", "0197_contact_einvoice_recipient")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
