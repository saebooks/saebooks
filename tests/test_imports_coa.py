"""Tests for CoA export / parse / diff / apply.

Parser + diff are pure, so most tests stay in-memory. The ``apply``
path writes to the DB under a scratch company so we don't pollute
the live seed — scrubbed in an autouse fixture.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.services.imports import coa as coa_svc
from saebooks.services.imports.coa import CoaImportError, CoaRow


async def _make_scratch_company() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = Company(
            name=f"imports-test-{uuid.uuid4().hex[:8]}",
            base_currency="AUD",
        )
        session.add(company)
        await session.commit()
        return company.id


async def _cleanup_company(company_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(Account).where(Account.company_id == company_id)
        )
        c = await session.get(Company, company_id)
        if c is not None:
            c.archived_at = datetime.now(UTC)
            await session.commit()


def test_parse_minimal_csv() -> None:
    raw = (
        "code,name,account_type,parent_code,tax_code_default,reconcile\n"
        "1-1100,Bank,ASSET,,,true\n"
        "4-1000,Sales,INCOME,,GST,false\n"
    )
    rows = coa_svc.parse_coa_csv(raw)
    assert len(rows) == 2
    assert rows[0].code == "1-1100"
    assert rows[0].account_type is AccountType.ASSET
    assert rows[0].reconcile is True
    assert rows[1].tax_code_default == "GST"


def test_parse_rejects_missing_columns() -> None:
    raw = "code,name\n1-1100,Bank\n"
    with pytest.raises(CoaImportError):
        coa_svc.parse_coa_csv(raw)


def test_parse_rejects_bad_account_type() -> None:
    raw = (
        "code,name,account_type\n"
        "1-1100,Bank,FRUITCAKE\n"
    )
    with pytest.raises(CoaImportError):
        coa_svc.parse_coa_csv(raw)


def test_parse_rejects_empty_required_field() -> None:
    raw = (
        "code,name,account_type\n"
        ",Bank,ASSET\n"
    )
    with pytest.raises(CoaImportError):
        coa_svc.parse_coa_csv(raw)


def test_diff_classifies_buckets() -> None:
    # Build an in-memory "existing" set via bare Account rows; diff_coa
    # only reads .code/.name/.account_type/.parent_id/.tax_code_default/
    # .reconcile so the objects don't need to be persisted.
    existing = [
        Account(
            id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            code="1-1100",
            name="Bank",
            account_type=AccountType.ASSET,
            tax_code_default=None,
            reconcile=True,
        ),
        Account(
            id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            code="4-1000",
            name="Sales",
            account_type=AccountType.INCOME,
            tax_code_default="GST",
            reconcile=False,
        ),
    ]
    rows = [
        CoaRow(  # unchanged
            code="1-1100",
            name="Bank",
            account_type=AccountType.ASSET,
            parent_code=None,
            tax_code_default=None,
            reconcile=True,
        ),
        CoaRow(  # changed (rename)
            code="4-1000",
            name="Sales Revenue",
            account_type=AccountType.INCOME,
            parent_code=None,
            tax_code_default="GST",
            reconcile=False,
        ),
        CoaRow(  # new
            code="6-1000",
            name="Advertising",
            account_type=AccountType.EXPENSE,
            parent_code=None,
            tax_code_default="GST",
            reconcile=False,
        ),
    ]
    diff = coa_svc.diff_coa(existing, rows)
    assert len(diff.unchanged) == 1
    assert len(diff.changed) == 1
    assert diff.changed[0][0].name == "Bank" or diff.changed[0][0].name == "Sales"
    assert len(diff.new) == 1
    assert diff.new[0].code == "6-1000"
    assert len(diff.removed) == 0


def test_diff_reports_removed() -> None:
    existing = [
        Account(
            id=uuid.uuid4(),
            company_id=uuid.uuid4(),
            code="1-1100",
            name="Bank",
            account_type=AccountType.ASSET,
            reconcile=True,
        ),
    ]
    diff = coa_svc.diff_coa(existing, [])
    assert len(diff.removed) == 1


@pytest.mark.asyncio
async def test_export_then_import_round_trip() -> None:
    """Export, parse the export, and diff — should produce all-unchanged."""
    cid = await _make_scratch_company()
    try:
        async with AsyncSessionLocal() as session:
            session.add_all(
                [
                    Account(
                        company_id=cid,
                        code="1-1100",
                        name="Bank",
                        account_type=AccountType.ASSET,
                        reconcile=True,
                    ),
                    Account(
                        company_id=cid,
                        code="4-1000",
                        name="Sales",
                        account_type=AccountType.INCOME,
                        reconcile=False,
                    ),
                ]
            )
            await session.commit()
            accounts = (
                await session.execute(
                    select(Account).where(Account.company_id == cid)
                )
            ).scalars().all()
        csv = coa_svc.export_coa_csv(list(accounts))
        rows = coa_svc.parse_coa_csv(csv)
        diff = coa_svc.diff_coa(list(accounts), rows)
        assert len(diff.new) == 0
        assert len(diff.changed) == 0
        assert len(diff.removed) == 0
        assert len(diff.unchanged) == 2
    finally:
        await _cleanup_company(cid)


@pytest.mark.asyncio
async def test_apply_creates_new_and_updates_changed() -> None:
    cid = await _make_scratch_company()
    try:
        # Seed one account to change.
        async with AsyncSessionLocal() as session:
            session.add(
                Account(
                    company_id=cid,
                    code="1-1100",
                    name="Bank Old Name",
                    account_type=AccountType.ASSET,
                    reconcile=False,
                )
            )
            await session.commit()

        rows = [
            CoaRow(
                code="1-1100",
                name="Bank",
                account_type=AccountType.ASSET,
                parent_code=None,
                tax_code_default=None,
                reconcile=True,
            ),
            CoaRow(
                code="4-1000",
                name="Sales",
                account_type=AccountType.INCOME,
                parent_code=None,
                tax_code_default=None,
                reconcile=False,
            ),
        ]
        async with AsyncSessionLocal() as session:
            existing = (
                await session.execute(
                    select(Account).where(Account.company_id == cid)
                )
            ).scalars().all()
            diff = coa_svc.diff_coa(list(existing), rows)
            applied = await coa_svc.apply_coa_diff(session, cid, diff)
            await session.commit()
        assert applied["new"] == 1
        assert applied["changed"] == 1

        async with AsyncSessionLocal() as session:
            refreshed = (
                await session.execute(
                    select(Account).where(Account.company_id == cid)
                )
            ).scalars().all()
            by_code = {a.code: a for a in refreshed}
            assert by_code["1-1100"].name == "Bank"
            assert by_code["1-1100"].reconcile is True
            assert by_code["4-1000"].account_type is AccountType.INCOME
    finally:
        await _cleanup_company(cid)


