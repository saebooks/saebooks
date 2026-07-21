"""Regression: the accounts(id, company_id) composite-FK target must exist on
every backend, including SQLite/Community bootstrap.

transfers.(from/to_account_id, company_id) and receipts.(bank_account_id,
company_id) FK to accounts(id, company_id). That target is the
``uq_accounts_id_company`` unique constraint. It lived only in a Postgres
raw-SQL migration (0152), not in the Account ORM __table_args__ — so SQLite's
bootstrap_schema (Base.metadata.create_all) never emitted the unique index and
every transfer/receipt insert died with SQLite "foreign key mismatch". The new
money-movement web UIs were therefore dead on Community.

These tests run on BOTH backends: a same-company transfer/receipt inserts
cleanly (no FK-mismatch), and a cross-company one is rejected by the composite
FK — the isolation guarantee the constraint exists to give.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.receipt import Receipt
from saebooks.models.transfer import Transfer

_DEFAULT_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_company(session, name: str) -> uuid.UUID:
    cid = uuid.uuid4()
    session.add(Company(id=cid, tenant_id=_DEFAULT_TENANT, name=name))
    await session.flush()
    return cid


async def _make_account(session, company_id: uuid.UUID, code: str) -> uuid.UUID:
    aid = uuid.uuid4()
    session.add(
        Account(
            id=aid,
            company_id=company_id,
            tenant_id=_DEFAULT_TENANT,
            code=code,
            name=f"Account {code}",
            account_type=AccountType.ASSET,
        )
    )
    await session.flush()
    return aid


async def test_same_company_transfer_inserts() -> None:
    """No 'foreign key mismatch' — the composite-FK target exists."""
    async with AsyncSessionLocal() as session:
        cid = await _make_company(session, "FK-Co-A")
        a1 = await _make_account(session, cid, "1000")
        a2 = await _make_account(session, cid, "1001")
        session.add(
            Transfer(
                tenant_id=_DEFAULT_TENANT,
                company_id=cid,
                from_account_id=a1,
                to_account_id=a2,
                amount=Decimal("50.00"),
                transfer_date=date(2026, 7, 22),
            )
        )
        await session.commit()  # must not raise


async def test_same_company_receipt_inserts() -> None:
    async with AsyncSessionLocal() as session:
        cid = await _make_company(session, "FK-Co-R")
        bank = await _make_account(session, cid, "1000")
        session.add(
            Receipt(
                tenant_id=_DEFAULT_TENANT,
                company_id=cid,
                bank_account_id=bank,
                number="RCT-0001",
                receipt_date=date(2026, 7, 22),
                total=Decimal("25.00"),
            )
        )
        await session.commit()  # must not raise


async def test_cross_company_transfer_rejected() -> None:
    """A transfer whose account belongs to a SISTER company must be rejected
    by the composite FK — the isolation guarantee uq_accounts_id_company gives."""
    async with AsyncSessionLocal() as session:
        cid_a = await _make_company(session, "FK-Co-X")
        cid_b = await _make_company(session, "FK-Co-Y")
        a_acct = await _make_account(session, cid_a, "1000")
        b_acct = await _make_account(session, cid_b, "1000")
        session.add(
            Transfer(
                tenant_id=_DEFAULT_TENANT,
                company_id=cid_a,
                from_account_id=a_acct,
                to_account_id=b_acct,  # belongs to company B, not A
                amount=Decimal("10.00"),
                transfer_date=date(2026, 7, 22),
            )
        )
        with pytest.raises((IntegrityError, OperationalError)):
            await session.commit()
