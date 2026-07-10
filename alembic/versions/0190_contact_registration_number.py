"""contacts.registration_number — counterparty registry-code column
(KMD-INF + TSD, Packet 1).

⚠ FIRST COMPANY-DB MIGRATION ON THIS LANE (kmd-inf-tsd scope §3.2). The
KMD-formula build deliberately touched no company-DB schema; this one
column is unavoidable for KMD-INF: Part A/B's counterparty grouping key
IS the partner's Estonian registry code (``registrikood``/``isikukood``)
and there is no existing ``Contact`` column that carries it (only
``abn``, AU-specific — ``models/contact.py``). Named generically
(``registration_number``, not ``ee_regcode``) to stay jurisdiction-
neutral, same convention as the rest of the engine (mirrors
``business_identifiers`` being a generic multi-scheme table rather than
one column per country).

Additive, nullable, no backfill, no server_default — existing rows
(every contact, every company, every jurisdiction) are unaffected.
Fully reversible via ``op.drop_column``.

Revision ID: 0190_contact_registration_number
Revises:     0189_scheduled_backups
Create Date: 2026-07-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0190_contact_registration_number"
down_revision: str | None = "0189_scheduled_backups"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "contacts"
_COLUMN = "registration_number"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.String(32),
            nullable=True,
            comment=(
                "Counterparty business registry code (e.g. Estonian "
                "registrikood/isikukood) — KMD-INF Part A/B grouping "
                "key. Jurisdiction-neutral; NULL means no code on file."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
