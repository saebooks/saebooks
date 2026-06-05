"""Migration 0156 structural assertions — FORCE RLS + grants + functions.

Proves the new-table RLS checklist holds for the principal grant table and
that the cross-tenant resolver functions exist and are SECURITY DEFINER.
Runs against the owner role (it inspects catalog tables, no RLS needed).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from saebooks.db import engine as _owner_engine

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]


async def test_grant_table_is_force_rls() -> None:
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = 'principal_tenant_grants'"
                )
            )
        ).first()
    assert row is not None, "principal_tenant_grants table missing"
    assert row.relrowsecurity is True, "RLS not enabled"
    assert row.relforcerowsecurity is True, "FORCE RLS not set"


async def test_grant_table_has_tenant_isolation_policy() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT policyname FROM pg_policies "
                    "WHERE tablename = 'principal_tenant_grants'"
                )
            )
        ).all()
    names = {r.policyname for r in rows}
    assert "tenant_isolation" in names, f"policy missing; have {names}"


async def test_resolver_functions_are_security_definer() -> None:
    async with _owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT proname, prosecdef FROM pg_proc "
                    "WHERE proname IN "
                    "('principal_visible_grants','principal_grant_role')"
                )
            )
        ).all()
    by_name = {r.proname: r.prosecdef for r in rows}
    assert by_name.get("principal_visible_grants") is True, (
        "principal_visible_grants must be SECURITY DEFINER"
    )
    assert by_name.get("principal_grant_role") is True, (
        "principal_grant_role must be SECURITY DEFINER"
    )


async def test_principals_table_not_rls() -> None:
    """principals is global — it has no tenant_id and must not be RLS'd
    (it is never read under a tenant session; only via the auth path /
    SECURITY DEFINER functions)."""
    async with _owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity FROM pg_class "
                    "WHERE relname = 'principals'"
                )
            )
        ).first()
    assert row is not None
    assert row.relrowsecurity is False
