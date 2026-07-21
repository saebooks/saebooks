"""RLS + grant checks for journal_line_tax_components (0180/0183/0184).

0180 created the table with RLS enabled/forced but — unlike every
sibling new-table migration since 0128 — never granted DML to the
non-superuser ``saebooks_app`` role that production runs the app
under. 0184 is the follow-up fix. This test pins the fix directly
against ``information_schema`` rather than exercising the full
role-flip probe (see ``tests/services/test_bank_routing_identifiers.py``
for that heavier pattern) — cheap and specific to the bug: it fails
loudly again if a future migration ever drops the grant.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

# Catalog-only reads (pg_class / pg_roles / has_table_privilege), so this
# works under any role — kept as the fixed owner engine (not
# saebooks.db.engine, which is the saebooks_app role under --rls) for
# consistency with the sibling RLS probe files.
from saebooks.db import _owner_role_engine as _owner_engine

pytestmark = pytest.mark.postgres_only

_TABLE = "journal_line_tax_components"


async def test_rls_enabled_and_forced() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = :t"
                ).bindparams(t=_TABLE)
            )
        ).one()
    assert row.relrowsecurity is True, "ROW LEVEL SECURITY not enabled"
    assert row.relforcerowsecurity is True, "FORCE ROW LEVEL SECURITY missing"


async def test_saebooks_app_role_granted_dml() -> None:
    """Regression pin for the missing-GRANT bug: without 0184, insert
    under the saebooks_app role fails with 'permission denied for table
    journal_line_tax_components' on any GST-bearing journal posting."""
    async with _owner_engine.connect() as conn:
        has_role = (
            await conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'saebooks_app'")
            )
        ).first()
        if has_role is None:
            pytest.skip("saebooks_app role missing — migration 0056 not applied")

        # has_table_privilege() is role-independent (unlike
        # information_schema.role_table_grants, which only surfaces rows
        # where the *connecting* role is grantor/grantee) — safe to check
        # from the owner engine regardless of who actually issued the
        # GRANT.
        missing = [
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            if not (
                await conn.execute(
                    text(
                        "SELECT has_table_privilege('saebooks_app', :t, :p)"
                    ).bindparams(t=_TABLE, p=privilege)
                )
            ).scalar()
        ]
    assert not missing, f"saebooks_app missing DML grants on {_TABLE}: {missing}"
