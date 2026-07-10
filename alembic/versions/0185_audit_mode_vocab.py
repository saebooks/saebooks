"""extended_audit_modes — rename ``companies.audit_mode`` vocabulary.

Wave C (planned-modules build-out, decision 8). ``CHARTER.md §7.2`` /
§12.1 defines exactly three audit modes: **immutable**, **open**,
**hybrid**. The column's original validator
(``services/companies.py``, pre-Wave-C) accepted a different,
undocumented set — ``{immutable, mutable, draft}`` — that predates the
CHARTER vocabulary and was never wired to any enforcement (see
``journal.py:245``'s orphaned global ``Setting`` read, fixed in the
same wave). This migration re-labels any existing data onto the
CHARTER vocabulary so the column and the code agree.

Mapping (FLAGGED — see below)
------------------------------
* ``immutable`` -> ``immutable``   (no change; CHARTER's default/only
  Community-tier value, unambiguous).
* ``mutable``   -> ``open``        (CHARTER "open": posted entries
  freely editable, every edit logged — "mutable" is the closest literal
  reading of the old label).
* ``draft``     -> ``hybrid``      (BEST-EFFORT GUESS, no other
  candidate fits: CHARTER "hybrid" = editable pre-period-close,
  immutable after. The old ``draft`` label doesn't map cleanly to
  either "fully open" or "immutable" — "not yet finalised" (draft) is
  closer in spirit to hybrid's "editable until the books close" than
  to unconditional openness, which is already claimed by the
  `mutable` mapping above. If Richard's actual intent for the old
  `draft` value differs, this is the one line to change — no other
  code depends on the specific mapping, and no row is dropped, only
  relabelled.
* Anything else (including NULL, which the column disallows — this
  is defensive only) -> ``immutable``, the fail-safe default matching
  CHARTER §6.1 ("Community ... immutable ledger only").

Whether any production row actually holds ``mutable``/``draft`` today
was NOT checked against the live database as part of building this
migration (by policy — building infra against a spec, not probing
prod mid-build). The validator that used to accept those two values
never had any caller writing them via the API (grep at build time:
zero UI/route wired to the audit_mode PATCH field before this wave),
so BOTH legacy values are expected to be inert at rest (only ever the
column default, ``'immutable'``, set at company-create time) — but the
migration handles the general case defensively regardless, and this
docstring flags the mapping choice for review either way.

Reversibility
-------------
``downgrade()`` maps back: ``open`` -> ``mutable``, ``hybrid`` ->
``draft``, ``immutable`` unchanged. Round-trips losslessly for any row
this migration itself touched, since the CHARTER vocabulary is a
strict relabelling (one legacy value per new value), not a merge.

Revision ID: 0185_audit_mode_vocab
Revises: 0184_jltc_grant_app_role
Create Date: 2026-07-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0185_audit_mode_vocab"
down_revision: str | None = "0184_jltc_grant_app_role"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE companies
            SET audit_mode = CASE audit_mode
                WHEN 'mutable' THEN 'open'
                WHEN 'draft'   THEN 'hybrid'
                WHEN 'immutable' THEN 'immutable'
                WHEN 'open'    THEN 'open'
                WHEN 'hybrid'  THEN 'hybrid'
                ELSE 'immutable'
            END
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE companies
            SET audit_mode = CASE audit_mode
                WHEN 'open'   THEN 'mutable'
                WHEN 'hybrid' THEN 'draft'
                ELSE audit_mode
            END
            """
        )
    )
