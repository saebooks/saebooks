"""0148_contact_is_one_off — add contacts.is_one_off flag.

Completes the half-built "one-off / walk-in contact" feature: the contacts
list template (list.html) renders a c.is_one_off badge, the page has an
All/Hide/Only filter, and a bulk-tag-one-off action toggles it — but the
backing column, model field, list filter and service wiring were never
added (TypeError 500 on the page; bulk-tag test red). This adds the column;
the model/service/endpoint changes land in the same commit.

NOT NULL + constant server_default false → catalog-only on PG16 (no rewrite);
existing contacts default to not-one-off.

Revision ID: 0148_contact_is_one_off
Revises: 0147_business_ident_coherence
Create Date: 2026-06-03
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0148_contact_is_one_off"
down_revision: str | None = "0147_business_ident_coherence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column(
            "is_one_off",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("contacts", "is_one_off")
