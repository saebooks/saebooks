"""Intercompany Phase 3c — REMOTE edge relay contra account.

Ported from ``feat/ic-remote-relay-live`` (originally cut as
``0161_ic_relay_contra``, down_revision ``0160_principal_webauthn_lookup``).
That branch predates main's ``saebooks/api/v1`` router refactor and its
migration number ``0161`` is now taken on main by
``0161_je_engine_guard``. Renumbered ``0217`` and re-chained onto main's
CURRENT single head (``0216_tax_return_amendment``) — do NOT reuse ``0161``.

Why this column exists
----------------------
A REMOTE relay leg is a balanced two-line JE: the edge-declared CONTROL account
(migration 0159's ``control_account_id`` — "Loan to SAE" / "Directors Loan
2-2200") and a CONTRA account (the side's own bank / clearing). To keep the
hard invariant that **no account id ever crosses the wire** (relay plan §4.3),
BOTH accounts must be resolvable from the receiver's OWN edge row — the partner
can never direct a posting into an arbitrary account of ours. 0159 gave the
control account a home; this adds the contra. It is composite-FK'd to
``accounts(id, company_id)`` so it can only ever be one of THIS edge's
company's own postable accounts.

Nullable + inert: LOCAL edges never use it (the LOCAL path takes the contra
from the caller); existing rows are untouched; the relay is flag-gated
default-off (``SAEBOOKS_IC_REMOTE_RELAY_ENABLED``).

Reversible: ``downgrade`` drops the column and its composite FK.

Revision ID: 0217_ic_relay_contra
Revises:     0216_tax_return_amendment
Create Date: 2026-07-16
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0217_ic_relay_contra"
down_revision: str | None = "0216_tax_return_amendment"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_FK_NAME = "fk_ic_edges_relay_contra_account_company"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(
        "ic_edges",
        sa.Column(
            "relay_contra_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    # Composite FK to accounts(id, company_id): the contra must belong to THIS
    # edge's company (same guard the 0154 control-account FK uses). Skipped on
    # SQLite (no composite-FK enforcement parity needed for single-tenant).
    if _is_postgres():
        op.create_foreign_key(
            _FK_NAME,
            "ic_edges",
            "accounts",
            ["relay_contra_account_id", "company_id"],
            ["id", "company_id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    if _is_postgres():
        op.drop_constraint(_FK_NAME, "ic_edges", type_="foreignkey")
    op.drop_column("ic_edges", "relay_contra_account_id")
