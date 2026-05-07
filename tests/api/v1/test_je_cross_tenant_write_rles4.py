"""Cross-tenant FK injection regression — journal entry lines.

The journal-entries API previously accepted JE lines that referenced
``account_id`` values from a different tenant — the JE itself sat in
tenant A but a line referenced an account from tenant B. ``services/
journal_entries.py`` now calls ``_validate_accounts_tenant`` before any
INSERT so foreign-tenant accounts are rejected. This file tests the
service layer directly (no RLS dependency) so the regression catches a
removal of the validation helpers even on the schema-owner role.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.tenant import Tenant
from saebooks.services import journal_entries as svc


# ---------------------------------------------------------------------------
# Two-tenant seed fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def rles4_seed() -> dict:
    """Create two tenants — apex (home) and walsh (foreign) — each with
    company + two accounts (ASSET + EXPENSE). Returns IDs for line assembly.
    """
    suffix = uuid.uuid4().hex[:8]
    out: dict = {}

    async with AsyncSessionLocal() as session:
        for label in ("apex", "walsh"):
            tenant_id = uuid.uuid4()
            company_id = uuid.uuid4()

            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"Test-{label}-{suffix}",
                    slug=f"rles4-{label}-{suffix}",
                )
            )
            await session.flush()

            session.add(
                Company(
                    id=company_id,
                    tenant_id=tenant_id,
                    name=f"Test-{label}-{suffix}",
                    base_currency="AUD",
                    fin_year_start_month=7,
                )
            )
            await session.flush()

            asset = Account(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                company_id=company_id,
                code=f"RL4{suffix[:3]}{label[0].upper()}A",
                name=f"Test Asset {label}",
                account_type=AccountType.ASSET,
            )
            expense = Account(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                company_id=company_id,
                code=f"RL4{suffix[:3]}{label[0].upper()}E",
                name=f"Test Expense {label}",
                account_type=AccountType.EXPENSE,
            )
            session.add_all([asset, expense])
            await session.flush()

            out[label] = {
                "tenant_id": tenant_id,
                "company_id": company_id,
                "asset_id": asset.id,
                "expense_id": expense.id,
            }

        await session.commit()

    yield out

    # Cleanup — JE lines hold FK onto accounts (no cascade DELETE in all
    # environments), so delete JEs first, then accounts, then companies, tenants.
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        for label in ("apex", "walsh"):
            ids = out[label]
            await session.execute(
                text(
                    "DELETE FROM journal_lines WHERE entry_id IN "
                    "(SELECT id FROM journal_entries WHERE company_id = :cid)"
                ),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM journal_entries WHERE company_id = :cid"),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM accounts WHERE company_id = :cid"),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM companies WHERE id = :cid"),
                {"cid": ids["company_id"]},
            )
            await session.execute(
                text("DELETE FROM tenants WHERE id = :tid"),
                {"tid": ids["tenant_id"]},
            )
        await session.commit()


# ---------------------------------------------------------------------------
# Positive control — same-tenant accounts accepted
# ---------------------------------------------------------------------------


async def test_je_create_same_tenant_succeeds(rles4_seed: dict) -> None:
    apex = rles4_seed["apex"]

    async with AsyncSessionLocal() as session:
        entry = await svc.create(
            session,
            apex["company_id"],
            apex["tenant_id"],
            actor="test:rles4-positive",
            entry_date=date(2026, 5, 1),
            narration="Same-tenant JE — must succeed",
            lines=[
                {"account_id": str(apex["asset_id"]), "debit": Decimal("500"), "credit": Decimal("0")},
                {"account_id": str(apex["expense_id"]), "debit": Decimal("0"), "credit": Decimal("500")},
            ],
        )

    assert entry is not None
    assert entry.tenant_id == apex["tenant_id"]
    assert len(entry.lines) == 2


# ---------------------------------------------------------------------------
# Negative — foreign-tenant account on a line is rejected
# ---------------------------------------------------------------------------


async def test_je_create_foreign_tenant_account_rejected(rles4_seed: dict) -> None:
    """Cross-tenant regression: a JE created under tenant A with a line that
    references an account from tenant B must raise JournalEntryError before
    any INSERT.
    """
    apex = rles4_seed["apex"]
    walsh = rles4_seed["walsh"]

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.JournalEntryError) as exc:
            await svc.create(
                session,
                apex["company_id"],
                apex["tenant_id"],
                actor="test:rles4-cross-tenant",
                entry_date=date(2026, 5, 1),
                narration="Cross-tenant attack (regression)",
                lines=[
                    # line 1: own-tenant debit (OK)
                    {"account_id": str(apex["asset_id"]), "debit": Decimal("500"), "credit": Decimal("0")},
                    # line 2: foreign-tenant account in current session (attack)
                    {"account_id": str(walsh["expense_id"]), "debit": Decimal("0"), "credit": Decimal("500")},
                ],
            )

    assert "do not belong" in str(exc.value).lower() or "tenant" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Negative — fully-random UUID for an account (unknown account, same error
# contract as the cross-tenant case so the API surface is consistent)
# ---------------------------------------------------------------------------


async def test_je_create_unknown_account_rejected(rles4_seed: dict) -> None:
    apex = rles4_seed["apex"]

    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.JournalEntryError):
            await svc.create(
                session,
                apex["company_id"],
                apex["tenant_id"],
                actor="test:rles4-unknown-acct",
                entry_date=date(2026, 5, 1),
                narration="Unknown account (regression negative control)",
                lines=[
                    {"account_id": str(apex["asset_id"]), "debit": Decimal("100"), "credit": Decimal("0")},
                    {"account_id": str(uuid.uuid4()), "debit": Decimal("0"), "credit": Decimal("100")},
                ],
            )


# ---------------------------------------------------------------------------
# Negative — foreign account on UPDATE (PATCH) path is also rejected
# ---------------------------------------------------------------------------


async def test_je_update_foreign_tenant_account_rejected(rles4_seed: dict) -> None:
    """regression: PATCH path must also validate account tenant ownership."""
    apex = rles4_seed["apex"]
    walsh = rles4_seed["walsh"]

    # First create a clean entry
    async with AsyncSessionLocal() as session:
        entry = await svc.create(
            session,
            apex["company_id"],
            apex["tenant_id"],
            actor="test:rles4-update-setup",
            entry_date=date(2026, 5, 1),
            narration="Setup for update test (regression)",
            lines=[
                {"account_id": str(apex["asset_id"]), "debit": Decimal("100"), "credit": Decimal("0")},
                {"account_id": str(apex["expense_id"]), "debit": Decimal("0"), "credit": Decimal("100")},
            ],
        )
        entry_id = entry.id
        version = entry.version

    # Attempt to replace lines with a foreign-tenant account
    async with AsyncSessionLocal() as session:
        with pytest.raises(svc.JournalEntryError) as exc:
            await svc.update(
                session,
                entry_id,
                actor="test:rles4-update-attack",
                expected_version=version,
                lines=[
                    {"account_id": str(apex["asset_id"]), "debit": Decimal("200"), "credit": Decimal("0")},
                    {"account_id": str(walsh["expense_id"]), "debit": Decimal("0"), "credit": Decimal("200")},
                ],
            )

    assert "do not belong" in str(exc.value).lower() or "tenant" in str(exc.value).lower()
