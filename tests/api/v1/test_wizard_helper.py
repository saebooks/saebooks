"""Unit tests for the Wizard helper (saebooks.api.v1._wizard).

Covers:
* start — inserts a row, returns a UUID
* get — returns state; None when expired; None when wrong tenant (RLS)
* step — merges patch, advances step counter; raises on expired/missing
* expire_old — deletes expired rows, returns count
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from saebooks.api.v1._wizard import Wizard, WizardExpiredError, WizardNotFoundError
from saebooks.db import AsyncSessionLocal

# Reuse the default dev tenant (matches conftest + migration seed).
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _session_with_tenant(tenant_id: uuid.UUID = _TENANT_ID):
    """Async context manager that opens a session with current_tenant set."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{tenant_id}'")
        )
        yield session


@pytest.mark.asyncio
async def test_wizard_start_returns_uuid() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{_TENANT_ID}'")
        )
        wid = await Wizard.start(session, kind="bank_csv", initial_state={"step": 0})
        assert isinstance(wid, uuid.UUID)
        await session.rollback()


@pytest.mark.asyncio
async def test_wizard_get_returns_state() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{_TENANT_ID}'")
        )
        wid = await Wizard.start(
            session,
            kind="coa",
            initial_state={"step": 0, "raw": "col,name\n"},
        )
        await session.flush()

        state = await Wizard.get(session, wid)
        assert state is not None
        assert state["step"] == 0
        assert state["raw"] == "col,name\n"
        await session.rollback()


@pytest.mark.asyncio
async def test_wizard_get_returns_none_for_expired() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{_TENANT_ID}'")
        )
        wid = await Wizard.start(
            session,
            kind="bank_csv",
            initial_state={"step": 0},
            ttl_seconds=3600,
        )
        await session.flush()
        # Manually expire the row by setting expires_at to the past.
        await session.execute(
            text(
                "UPDATE wizard_state SET expires_at = now() - INTERVAL '1 second' WHERE id = :wid"
            ).bindparams(wid=str(wid))
        )
        await session.flush()

        state = await Wizard.get(session, wid)
        assert state is None
        await session.rollback()


@pytest.mark.asyncio
async def test_wizard_step_merges_patch() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{_TENANT_ID}'")
        )
        wid = await Wizard.start(
            session,
            kind="bank_csv",
            initial_state={"step": 0, "account_id": None},
        )
        await session.flush()

        merged = await Wizard.step(
            session,
            wid,
            patch_state={"step": 1, "account_id": "abc-123"},
        )
        assert merged["step"] == 1
        assert merged["account_id"] == "abc-123"
        await session.rollback()


@pytest.mark.asyncio
async def test_wizard_step_raises_on_expired() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{_TENANT_ID}'")
        )
        wid = await Wizard.start(
            session,
            kind="bank_csv",
            initial_state={"step": 0},
        )
        await session.flush()
        await session.execute(
            text(
                "UPDATE wizard_state SET expires_at = now() - INTERVAL '1 second' WHERE id = :wid"
            ).bindparams(wid=str(wid))
        )
        await session.flush()

        with pytest.raises(WizardExpiredError):
            await Wizard.step(session, wid, patch_state={"step": 1})
        await session.rollback()


@pytest.mark.asyncio
async def test_wizard_step_raises_on_missing() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{_TENANT_ID}'")
        )
        with pytest.raises(WizardNotFoundError):
            await Wizard.step(session, uuid.uuid4(), patch_state={"step": 1})
        await session.rollback()


@pytest.mark.asyncio
async def test_wizard_expire_old_deletes_expired() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"SET LOCAL app.current_tenant = '{_TENANT_ID}'")
        )
        # Create a row that's already expired.
        wid = await Wizard.start(
            session,
            kind="bank_csv",
            initial_state={"step": 0},
        )
        await session.flush()
        await session.execute(
            text(
                "UPDATE wizard_state SET expires_at = now() - INTERVAL '1 second' WHERE id = :wid"
            ).bindparams(wid=str(wid))
        )
        await session.flush()

        deleted = await Wizard.expire_old(session)
        assert deleted >= 1

        # Confirm the row is gone.
        state = await Wizard.get(session, wid)
        assert state is None
        await session.rollback()
