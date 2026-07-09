"""DB-layer tests for the cashbook_default_bank_account_id CHECK constraint.

Critic finding #29 (2026-05-23): some companies had bookkeeping_mode='full'
with a non-null cashbook_default_bank_account_id.  Fix: migration 0126 adds

    CHECK (cashbook_default_bank_account_id IS NULL
           OR bookkeeping_mode = 'cashbook')

and nulls any existing offenders.  These tests assert:

1. A full-mode company with the field set raises IntegrityError (constraint
   is live in the test DB).
2. A cashbook-mode company with the field set is accepted.
3. A full-mode company with the field null is accepted (normal case).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from saebooks.db import AsyncSessionLocal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_account_id(session) -> uuid.UUID:
    """Return any account id from the test DB to use as FK target."""
    result = await session.execute(
        text("SELECT id FROM accounts LIMIT 1")
    )
    row = result.fetchone()
    if row is None:
        pytest.skip("No accounts in test DB — seed needed")
    return row[0]


async def _get_seed_company_id(session) -> uuid.UUID:
    """Return the seed company id."""
    result = await session.execute(
        text("SELECT id FROM companies ORDER BY created_at LIMIT 1")
    )
    row = result.fetchone()
    if row is None:
        pytest.skip("No companies in test DB — seed needed")
    return row[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_full_mode_with_bank_id_violates_constraint() -> None:
    """Setting cashbook_default_bank_account_id on a full-mode company must
    raise IntegrityError from the CHECK constraint."""
    async with AsyncSessionLocal() as session:
        acct_id = await _seed_account_id(session)
        company_id = await _get_seed_company_id(session)

        with pytest.raises(IntegrityError, match="ck_cashbook_default_bank_requires_cashbook_mode"):
            await session.execute(
                text(
                    "UPDATE companies "
                    "SET cashbook_default_bank_account_id = :acct "
                    "WHERE id = :cid AND bookkeeping_mode = 'full'"
                ).bindparams(acct=acct_id, cid=company_id)
            )
            await session.flush()
        await session.rollback()


async def test_full_mode_with_null_bank_id_is_valid() -> None:
    """A full-mode company with NULL cashbook_default_bank_account_id is fine."""
    async with AsyncSessionLocal() as session:
        company_id = await _get_seed_company_id(session)
        # Ensure it is in full mode and field is null — the seed company
        # should already be in this state; we just confirm.
        await session.execute(
            text(
                "UPDATE companies "
                "SET bookkeeping_mode = 'full', "
                "cashbook_default_bank_account_id = NULL "
                "WHERE id = :cid"
            ).bindparams(cid=company_id)
        )
        await session.flush()
        # No exception raised means the constraint is satisfied.
        await session.rollback()


async def test_cashbook_mode_with_bank_id_is_valid() -> None:
    """A cashbook-mode company with a non-null bank account id satisfies
    both the existing ck_cashbook_requires_bank constraint (mode=cashbook
    requires non-null) and the new complement constraint."""
    async with AsyncSessionLocal() as session:
        acct_id = await _seed_account_id(session)
        company_id = await _get_seed_company_id(session)

        await session.execute(
            text(
                "UPDATE companies "
                "SET bookkeeping_mode = 'cashbook', "
                "cashbook_default_bank_account_id = :acct "
                "WHERE id = :cid"
            ).bindparams(acct=acct_id, cid=company_id)
        )
        await session.flush()
        # No exception raised means both constraints are satisfied.
        await session.rollback()


async def test_constraint_name_exists_in_db() -> None:
    """Verify the constraint was applied by querying pg_constraint."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'companies'::regclass "
                "AND conname = 'ck_cashbook_default_bank_requires_cashbook_mode'"
            )
        )
        row = result.fetchone()
        assert row is not None, (
            "Constraint ck_cashbook_default_bank_requires_cashbook_mode "
            "not found in pg_constraint — run alembic upgrade head"
        )
