"""GRANT saebooks_app on journal_line_tax_components (M1.5 hardening).

0180 created journal_line_tax_components with RLS enabled/forced but
never issued the explicit ``GRANT SELECT, INSERT, UPDATE, DELETE ...
TO saebooks_app`` that every other new-table migration since 0128 has
carried (0154/0155/0157/0158/0159/0174/0175/0178/0182). Per 0128's own
docstring, the catch-all ``ALTER DEFAULT PRIVILEGES`` set up in 0056
only covers tables created by the SAME role that issued it — a table
created under the migration-runner role is silently excluded, and the
non-superuser ``saebooks_app`` role production runs as
(``saebooks/db.py`` / ``SAEBOOKS_APP_DATABASE_URL``) gets "permission
denied for table journal_line_tax_components" the first time
``services/journal.py``'s ``_apply_tax_treatment`` flushes a
``JournalLineTaxComponent`` row — i.e. on any normal GST-bearing
posting. Fixed as a follow-up migration (0180 may already be applied
in some environments) rather than editing 0180 in place, same posture
as 0183's coherence-trigger retrofit on this same table.

Revision ID: 0184_jltc_grant_app_role
Revises: 0183_jltc_coherence_trigger
Create Date: 2026-07-09
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0184_jltc_grant_app_role"
down_revision: str | None = "0183_jltc_coherence_trigger"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "journal_line_tax_components"


def _grant_app(table: str) -> None:
    op.execute(
        sa.text(
            f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO saebooks_app;
                END IF;
            END $$;
            """
        )
    )


def _revoke_app(table: str) -> None:
    op.execute(
        sa.text(
            f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN
                    REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM saebooks_app;
                END IF;
            END $$;
            """
        )
    )


def upgrade() -> None:
    _grant_app(_TABLE)


def downgrade() -> None:
    _revoke_app(_TABLE)
