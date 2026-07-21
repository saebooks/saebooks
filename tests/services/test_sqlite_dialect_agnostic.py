"""Regression: engine paths that used Postgres-only SQL must work on the
SQLite/Community backend.

Covers the #5 sweep:
* ``settings_svc.set`` — was ``sqlalchemy.dialects.postgresql.insert`` (upsert),
  so every company-settings write 500'd on SQLite. Now routed through the
  dialect-agnostic ``upsert_stmt`` helper.
* ``journal_entries.get_source_doc`` + ``_check_expenses_table`` — used
  ``id::text`` / ``left()`` / ``to_regclass('public.expenses')`` (all
  Postgres-only), so every journal-entry detail view 500'd on SQLite. Now uses
  CAST(... AS TEXT) / substr() / the SQLAlchemy inspector.

Both run on Postgres too (the constructs are cross-dialect), so no marker.
"""
from __future__ import annotations

import uuid

from saebooks.db import AsyncSessionLocal
from saebooks.services import journal_entries as je_svc
from saebooks.services import settings as settings_svc

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def test_settings_set_and_get_round_trip() -> None:
    key = f"test.key.{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        await settings_svc.set(session, key, {"enabled": True}, updated_by="tester")
    async with AsyncSessionLocal() as session:
        assert await settings_svc.get(session, key) == {"enabled": True}


async def test_settings_set_is_idempotent_upsert() -> None:
    """A second set on the same key updates in place (on_conflict path)."""
    key = f"test.key.{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        await settings_svc.set(session, key, {"v": 1})
    async with AsyncSessionLocal() as session:
        await settings_svc.set(session, key, {"v": 2})
    async with AsyncSessionLocal() as session:
        assert await settings_svc.get(session, key) == {"v": 2}


async def test_get_source_doc_runs_and_returns_none_for_unlinked_je() -> None:
    """The cross-dialect source-lookup SQL executes on SQLite (previously it
    raised on ``id::text`` / ``left()`` / ``to_regclass``)."""
    async with AsyncSessionLocal() as session:
        result = await je_svc.get_source_doc(
            session, uuid.uuid4(), tenant_id=_DEFAULT_TENANT
        )
        assert result is None
