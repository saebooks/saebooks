"""Backend-agnostic Wizard helper coverage — the SQLite (Cashbook/Community)
regression guard for the import wizard.

``tests/api/v1/test_wizard_helper.py`` is ``postgres_only`` (it binds the
tenant with ``SET LOCAL app.current_tenant`` and drives the raw-SQL path).
That left the SQLite Cashbook / Community backend — the free single-device
edition shipped by ``docker-compose.community.yml`` and the one-click
installer — with ZERO wizard coverage, and the Postgres-only SQL in
``Wizard`` (``current_setting(...)::uuid``, ``CAST(:state AS jsonb)``,
JSONB ``||`` merge) failed at runtime on SQLite with
``unrecognized token: ":"`` — so bank statement import was completely
broken there.

These tests bind the tenant via ``session.info['tenant_id']`` (the
``tenant_session`` helper), which the ``get_session`` request path also
uses, so they run on BOTH backends: on Postgres they exercise the raw-SQL
path, on SQLite the dialect-agnostic ORM path added alongside it. Run the
SQLite arm with ``DATABASE_URL=sqlite+aiosqlite:///…``.
"""
from __future__ import annotations

import uuid

import pytest

from saebooks.api.v1._wizard import Wizard, WizardExpiredError, WizardNotFoundError
from tests.conftest import tenant_session

pytestmark = pytest.mark.asyncio

# Default dev tenant — seeded by the migration (PG) and by bootstrap_schema (SQLite).
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def test_start_then_get_roundtrip() -> None:
    async with tenant_session(_TENANT_ID) as session:
        wid = await Wizard.start(
            session,
            kind="bank_csv",
            initial_state={"step": 0, "account_id": "acc-1"},
        )
        assert isinstance(wid, uuid.UUID)
        await session.flush()

        state = await Wizard.get(session, wid)
        assert state is not None
        assert state["account_id"] == "acc-1"
        # `kind` is mirrored into state so a commit can dispatch off it.
        assert state["kind"] == "bank_csv"
        await session.rollback()


async def test_step_merges_and_preserves_unmentioned_keys() -> None:
    async with tenant_session(_TENANT_ID) as session:
        wid = await Wizard.start(
            session,
            kind="bank_csv",
            initial_state={"step": 0, "account_id": "acc-9"},
        )
        await session.flush()

        merged = await Wizard.step(
            session,
            wid,
            patch_state={"step": 1, "raw": "Date,Amount\n2026-06-01,10.00", "_completed": True},
        )
        # Patched keys applied…
        assert merged["step"] == 1
        assert merged["_completed"] is True
        assert merged["raw"].startswith("Date,Amount")
        # …unmentioned keys preserved (the JSONB `||` / Python-merge invariant).
        assert merged["account_id"] == "acc-9"
        assert merged["kind"] == "bank_csv"
        await session.rollback()


async def test_step_raises_on_missing() -> None:
    async with tenant_session(_TENANT_ID) as session:
        with pytest.raises(WizardNotFoundError):
            await Wizard.step(session, uuid.uuid4(), patch_state={"step": 1})
        await session.rollback()


async def test_get_returns_none_for_expired() -> None:
    async with tenant_session(_TENANT_ID) as session:
        # ttl in the past → immediately expired.
        wid = await Wizard.start(
            session,
            kind="bank_csv",
            initial_state={"step": 0},
            ttl_seconds=-1,
        )
        await session.flush()
        assert await Wizard.get(session, wid) is None
        with pytest.raises(WizardExpiredError):
            await Wizard.step(session, wid, patch_state={"step": 1})
        await session.rollback()


async def test_list_active_and_expire_old() -> None:
    async with tenant_session(_TENANT_ID) as session:
        live = await Wizard.start(
            session, kind="bank_csv", initial_state={"step": 0}, ttl_seconds=3600
        )
        dead = await Wizard.start(
            session, kind="bank_csv", initial_state={"step": 0}, ttl_seconds=-1
        )
        await session.flush()

        listed = await Wizard.list_active(session, kind="bank_csv")
        ids = {row["wizard_id"] for row in listed}
        assert str(live) in ids
        assert str(dead) not in ids  # expired rows are excluded

        deleted = await Wizard.expire_old(session)
        assert deleted >= 1
        assert await Wizard.get(session, dead) is None
        await session.rollback()
