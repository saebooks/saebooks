"""bsl_matches.matched_via + rule_id — match provenance (M3 R8b).

Adds two additive columns to the existing ``bsl_matches`` junction
(migration 0082 — already the reconciliation source of truth):

* ``matched_via`` VARCHAR(16) NOT NULL DEFAULT ``'MANUAL'`` — one of
  MANUAL / AUTO / RULE / COMPOUND. Every existing row backfills to
  MANUAL via the column default, which is the correct provenance for
  all matches created before this revision (they were all created via
  the manual ``/reconciliation/match`` or ``split_match`` flows).
* ``rule_id`` UUID, nullable, FK -> ``bank_rules(id)`` ON DELETE SET
  NULL — set when a bank rule drove the match (``matched_via='RULE'``
  or an auto-match whose scoring hit rule pattern RULE_PATTERN);
  ``NULL`` otherwise. ``SET NULL`` (not CASCADE) so deleting a rule
  never destroys reconciliation history — it just drops the
  provenance pointer.

``bsl_matches`` is an existing CompanyScoped/tenant-scoped table with
FORCE RLS + an isolation policy already in place (migration 0082); this
is a plain additive column pair on an already-scoped table, so the
new-tenant-table RLS checklist does not apply here.

Revision ID: 0220_bsl_match_provenance
Revises: 0219_tpar_bde_fields
Create Date: 2026-07-18
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0220_bsl_match_provenance"
down_revision: str | None = "0219_tpar_bde_fields"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "bsl_matches"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "matched_via",
            sa.String(16),
            nullable=False,
            server_default="MANUAL",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "bsl_matches_rule_id_fkey",
        _TABLE,
        "bank_rules",
        ["rule_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "bsl_matches_matched_via_check",
        _TABLE,
        "matched_via IN ('MANUAL', 'AUTO', 'RULE', 'COMPOUND')",
    )
    op.create_index("ix_bsl_matches_rule_id", _TABLE, ["rule_id"])


def downgrade() -> None:
    op.drop_index("ix_bsl_matches_rule_id", table_name=_TABLE)
    op.drop_constraint("bsl_matches_matched_via_check", _TABLE, type_="check")
    op.drop_constraint("bsl_matches_rule_id_fkey", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "rule_id")
    op.drop_column(_TABLE, "matched_via")
