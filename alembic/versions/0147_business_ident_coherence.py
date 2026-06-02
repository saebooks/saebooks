"""0147_business_ident_coherence — tenant-coherence trigger (+ grant) on business_identifiers.

0145 created ``business_identifiers`` with a denormalised ``tenant_id`` alongside
``company_id``, FORCE RLS, and a ``tenant_isolation`` policy — but WITHOUT the
BEFORE INSERT/UPDATE tenant-coherence trigger that every other denormalised child
table carries (0131 for the original eight; 0137 for the one-off party tables).
That is the one item the project RLS checklist flagged as missing on 0145.

This migration closes the gap so the table fully passes the checklist:

    NEW.tenant_id MUST equal (SELECT tenant_id FROM companies WHERE id = NEW.company_id)

A row whose tenant_id disagrees with its company's tenant_id can never be stored.
The trigger reuses the shared function ``assert_child_tenant_matches_company()``
defined (CREATE OR REPLACE) in 0131, so it is guaranteed present at this point.

Also adds an explicit, guarded GRANT to the NOBYPASSRLS app role ``saebooks_app``
(belt-and-braces, matching 0138_tpar). On stacks where the migration role is the
0056 default-privileges grantor this is redundant, but the explicit grant is
unambiguous across role/stack differences. Guarded on role existence so it is a
no-op where ``saebooks_app`` is not provisioned.

Reversibility: ``downgrade()`` drops the trigger (the shared function and the
grant are left in place — both are harmless and shared/owned elsewhere).

NOTE: revision id is kept <= 32 chars because alembic_version.version_num is
VARCHAR(32); the descriptive table/trigger names below are unaffected by that cap.

Revision ID: 0147_business_ident_coherence
Revises: 0146_companies_coa_template_key
Create Date: 2026-06-03
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0147_business_ident_coherence"
down_revision: str | None = "0146_companies_coa_template_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "business_identifiers"
_FN_NAME = "assert_child_tenant_matches_company"
_TRIGGER = f"trg_{_TABLE}_tenant_coherence"


def upgrade() -> None:
    # Coherence trigger — idempotent (DROP IF EXISTS first), one statement per
    # op.execute so asyncpg never sees multiple statements in one prepared stmt.
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON {_TABLE}"))
    op.execute(
        sa.text(
            f"CREATE TRIGGER {_TRIGGER} "
            f"BEFORE INSERT OR UPDATE ON {_TABLE} "
            f"FOR EACH ROW EXECUTE FUNCTION {_FN_NAME}()"
        )
    )

    # Belt-and-braces explicit grant to the NOBYPASSRLS app role (guarded on
    # role existence; GRANT is permitted directly inside a PL/pgSQL DO block).
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app') THEN "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO saebooks_app; "
            "END IF; END $$"
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON {_TABLE}"))
