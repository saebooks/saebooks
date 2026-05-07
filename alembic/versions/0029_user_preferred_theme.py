"""User preferred theme — per-user CSS-bundle override (Batch QQ)

Revision ID: 0029_user_preferred_theme
Revises: 0028_inventory
Create Date: 2026-04-21

Adds a nullable ``preferred_theme`` column to ``users``. Null means
"use the company-wide theme" (driven by ``SAEBOOKS_FRONTEND`` env or
the ``theme`` row in ``settings``). A non-null value picks the
per-user CSS bundle loaded at the top of every page — the Jinja
template tree is still global (see ``saebooks/web.py``), so only the
visual styling swaps, not the page structure.

Validated against ``services.theme.ACTIVE_THEMES`` at write time; we
don't add a DB check constraint because the set of themes is expected
to grow and shrink with deployment config.

Additive: existing rows stay at ``NULL`` which is exactly the
pre-QQ behaviour.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_user_preferred_theme"
down_revision: str | None = "0028_inventory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("preferred_theme", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "preferred_theme")
