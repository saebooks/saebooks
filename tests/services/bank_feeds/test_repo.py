"""Tests for saebooks.services.bank_feeds.repo.

Covers the idempotency guarantees of the persistence layer:

- ``get_or_create_client`` is idempotent; raises on re-link attempts.
- ``upsert_bank_feed_account`` upserts on ``sds_account_id``.
- ``insert_statement_lines`` dedupes on ``(bank_feed_account_id, external_id)``.
- ``upsert_feed_issue`` updates the existing row when status flips.
- Parser helpers tolerate the mixed date/datetime forms the aggregator emits.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.bank_feed import (
    BankFeedAccount,
    BankFeedClient,
    BankFeedIssue,
    BankFeedIssueStatus,
)
from saebooks.models.bank_statement import BankStatementLine
from saebooks.models.company import Company
from saebooks.services.bank_feeds import repo

pytestmark = pytest.mark.postgres_only


async def _seed_ctx() -> tuple[uuid.UUID, uuid.UUID]:
    """Return (company_id, ledger_account_id) from the seeded fixtures.

    Reuses the same approach as tests/test_journal.py — the seeded demo
    company + CoA rows are assumed present.
    """
    async with AsyncSessionLocal() as session:
        co = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = co.scalars().first()
        assert company is not None

        acct = await session.execute(
            select(Account).where(Account.company_id == company.id).order_by(Account.code)
        )
        first = acct.scalars().first()
        assert first is not None
        return company.id, first.id


def _tag() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------- #
# Client                                                                 #
# ---------------------------------------------------------------------- #


async def test_get_or_create_client_is_idempotent() -> None:
    company_id, _ = await _seed_ctx()
    sds_id = f"sds-client-{_tag()}"
    async with AsyncSessionLocal() as session:
        try:
            row = await repo.get_or_create_client(
                session, company_id=company_id, sds_client_id=sds_id
            )
            assert row.sds_client_id == sds_id

            again = await repo.get_or_create_client(
                session, company_id=company_id, sds_client_id=sds_id
            )
            assert again.id == row.id
            await session.commit()
        finally:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(
                    delete(BankFeedClient).where(BankFeedClient.company_id == company_id)
                )
                await cleanup.commit()


async def test_get_or_create_client_refuses_relink() -> None:
    company_id, _ = await _seed_ctx()
    first = f"sds-client-{_tag()}"
    second = f"sds-client-{_tag()}"
    async with AsyncSessionLocal() as session:
        try:
            await repo.get_or_create_client(
                session, company_id=company_id, sds_client_id=first
            )
            await session.commit()

            with pytest.raises(ValueError, match="refusing to re-link"):
                await repo.get_or_create_client(
                    session, company_id=company_id, sds_client_id=second
                )
        finally:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(
                    delete(BankFeedClient).where(BankFeedClient.company_id == company_id)
                )
                await cleanup.commit()


# ---------------------------------------------------------------------- #
# Account upsert                                                         #
# ---------------------------------------------------------------------- #


async def test_upsert_bank_feed_account_is_idempotent_and_updates() -> None:
    company_id, ledger_id = await _seed_ctx()
    sds_client_id = f"sds-client-{_tag()}"
    sds_account_id = f"acct-{_tag()}"
    async with AsyncSessionLocal() as session:
        try:
            client = await repo.get_or_create_client(
                session, company_id=company_id, sds_client_id=sds_client_id
            )

            payload_v1: dict[str, Any] = {
                "accountId": sds_account_id,
                "displayName": "Everyday",
                "maskedNumber": "xxxxx1234",
                "productCategory": "TRANS_AND_SAVINGS_ACCOUNTS",
                "sds": {
                    "sdsInstitutionId": "INST1",
                    "feedType": "DIRECT_FEED",
                    "processingStatus": "A",
                    "processingStatusDate": "2026-04-01",
                    "lastTransactionPostedId": "tx-1",
                    "lastTransactionPostedDate": "2026-04-10",
                },
            }
            first = await repo.upsert_bank_feed_account(
                session,
                company_id=company_id,
                bank_feed_client_id=client.id,
                ledger_account_id=ledger_id,
                account=payload_v1,
            )
            assert first.display_name == "Everyday"
            assert first.processing_status == "A"

            # Same account id → second call should update the row in place.
            payload_v2 = {
                **payload_v1,
                "displayName": "Everyday Offset",
                "sds": {
                    **payload_v1["sds"],
                    "processingStatus": "B",
                    "lastTransactionPostedId": "tx-2",
                    "lastTransactionPostedDate": "2026-04-11",
                },
            }
            second = await repo.upsert_bank_feed_account(
                session,
                company_id=company_id,
                bank_feed_client_id=client.id,
                ledger_account_id=ledger_id,
                account=payload_v2,
            )
            assert second.id == first.id
            assert second.display_name == "Everyday Offset"
            assert second.processing_status == "B"
            assert second.last_transaction_posted_id == "tx-2"
            assert second.last_transaction_posted_date == date(2026, 4, 11)
            await session.commit()
        finally:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(
                    delete(BankStatementLine).where(
                        BankStatementLine.company_id == company_id
                    )
                )
                await cleanup.execute(
                    delete(BankFeedAccount).where(
                        BankFeedAccount.sds_account_id == sds_account_id
                    )
                )
                await cleanup.execute(
                    delete(BankFeedClient).where(BankFeedClient.company_id == company_id)
                )
                await cleanup.commit()


# ---------------------------------------------------------------------- #
# Statement line dedup                                                   #
# ---------------------------------------------------------------------- #


async def test_insert_statement_lines_is_idempotent() -> None:
    company_id, ledger_id = await _seed_ctx()
    sds_client_id = f"sds-client-{_tag()}"
    sds_account_id = f"acct-{_tag()}"
    async with AsyncSessionLocal() as session:
        try:
            client = await repo.get_or_create_client(
                session, company_id=company_id, sds_client_id=sds_client_id
            )
            account = await repo.upsert_bank_feed_account(
                session,
                company_id=company_id,
                bank_feed_client_id=client.id,
                ledger_account_id=ledger_id,
                account={
                    "accountId": sds_account_id,
                    "sds": {"sdsInstitutionId": "INST1"},
                },
            )
            await session.commit()

            txns = [
                {
                    "transactionId": "txn-A",
                    "postingDateTime": "2026-04-10T09:00:00Z",
                    "amount": "125.50",
                    "description": "Coffee",
                },
                {
                    "transactionId": "txn-B",
                    "postingDateTime": "2026-04-11T10:00:00+10:00",
                    "amount": "-42.00",
                    "description": "Parking",
                },
            ]

            first = await repo.insert_statement_lines(
                session, bank_feed_account_id=account.id, transactions=txns
            )
            await session.commit()
            assert first == 2

            # Same payload again → zero new rows inserted.
            second = await repo.insert_statement_lines(
                session, bank_feed_account_id=account.id, transactions=txns
            )
            await session.commit()
            assert second == 0

            # A new transaction plus the two existing ones → exactly 1 insert.
            mixed = [
                *txns,
                {
                    "transactionId": "txn-C",
                    "executionDateTime": "2026-04-12T11:00:00Z",
                    "amount": "7.77",
                    "description": "New one",
                },
            ]
            third = await repo.insert_statement_lines(
                session, bank_feed_account_id=account.id, transactions=mixed
            )
            await session.commit()
            assert third == 1

            rows = await session.execute(
                select(BankStatementLine).where(
                    BankStatementLine.bank_feed_account_id == account.id
                )
            )
            persisted = rows.scalars().all()
            assert {r.external_id for r in persisted} == {"txn-A", "txn-B", "txn-C"}
            amounts = {r.external_id: r.amount for r in persisted}
            assert amounts["txn-A"] == Decimal("125.50")
            assert amounts["txn-B"] == Decimal("-42.00")
        finally:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(
                    delete(BankStatementLine).where(
                        BankStatementLine.bank_feed_account_id == account.id
                    )
                )
                await cleanup.execute(
                    delete(BankFeedAccount).where(BankFeedAccount.id == account.id)
                )
                await cleanup.execute(
                    delete(BankFeedClient).where(BankFeedClient.id == client.id)
                )
                await cleanup.commit()


async def test_insert_statement_lines_empty_is_noop() -> None:
    async with AsyncSessionLocal() as session:
        inserted = await repo.insert_statement_lines(
            session, bank_feed_account_id=uuid.uuid4(), transactions=[]
        )
        assert inserted == 0


# ---------------------------------------------------------------------- #
# Sync cursor                                                            #
# ---------------------------------------------------------------------- #


async def test_update_sync_cursor_persists() -> None:
    company_id, ledger_id = await _seed_ctx()
    sds_client_id = f"sds-client-{_tag()}"
    sds_account_id = f"acct-{_tag()}"
    async with AsyncSessionLocal() as session:
        try:
            client = await repo.get_or_create_client(
                session, company_id=company_id, sds_client_id=sds_client_id
            )
            account = await repo.upsert_bank_feed_account(
                session,
                company_id=company_id,
                bank_feed_client_id=client.id,
                ledger_account_id=ledger_id,
                account={
                    "accountId": sds_account_id,
                    "sds": {"sdsInstitutionId": "INST1"},
                },
            )
            await session.commit()

            await repo.update_sync_cursor(
                session,
                bank_feed_account_id=account.id,
                last_transaction_posted_id="tx-999",
                last_transaction_posted_date=date(2026, 4, 15),
            )
            await session.commit()

            refreshed = await session.get(BankFeedAccount, account.id)
            assert refreshed is not None
            assert refreshed.last_transaction_posted_id == "tx-999"
            assert refreshed.last_transaction_posted_date == date(2026, 4, 15)
        finally:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(
                    delete(BankFeedAccount).where(BankFeedAccount.id == account.id)
                )
                await cleanup.execute(
                    delete(BankFeedClient).where(BankFeedClient.id == client.id)
                )
                await cleanup.commit()


# ---------------------------------------------------------------------- #
# Feed issues                                                            #
# ---------------------------------------------------------------------- #


async def test_upsert_feed_issue_transitions_active_to_closed() -> None:
    feed_issue_id = f"issue-{_tag()}"
    async with AsyncSessionLocal() as session:
        try:
            first = await repo.upsert_feed_issue(
                session,
                issue={
                    "feedIssueId": feed_issue_id,
                    "sdsInstitutionId": "INST1",
                    "status": "active",
                    "creationDateTime": "2026-04-01T00:00:00Z",
                    "lastMessage": "Investigating",
                    "country": "AU",
                },
            )
            assert first.status == BankFeedIssueStatus.ACTIVE
            await session.commit()

            second = await repo.upsert_feed_issue(
                session,
                issue={
                    "feedIssueId": feed_issue_id,
                    "sdsInstitutionId": "INST1",
                    "status": "closed",
                    "creationDateTime": "2026-04-01T00:00:00Z",
                    "closedDateTime": "2026-04-02T05:00:00Z",
                    "lastMessage": "Resolved",
                    "country": "AU",
                },
            )
            assert second.id == first.id
            assert second.status == BankFeedIssueStatus.CLOSED
            assert second.closed_datetime is not None
            assert second.last_message == "Resolved"
            await session.commit()
        finally:
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(
                    delete(BankFeedIssue).where(
                        BankFeedIssue.sds_feed_issue_id == feed_issue_id
                    )
                )
                await cleanup.commit()


# ---------------------------------------------------------------------- #
# Parsers                                                                #
# ---------------------------------------------------------------------- #


def test_parse_date_handles_mixed_inputs() -> None:
    assert repo._parse_date(None) is None
    assert repo._parse_date("") is None
    assert repo._parse_date("2026-04-10") == date(2026, 4, 10)
    assert repo._parse_date("2026-04-10T09:00:00Z") == date(2026, 4, 10)
    assert repo._parse_date("2026-04-10T09:00:00+10:00") == date(2026, 4, 10)
    assert repo._parse_date(date(2026, 4, 10)) == date(2026, 4, 10)
    assert repo._parse_date(datetime(2026, 4, 10, 9, 0, 0)) == date(2026, 4, 10)
    assert repo._parse_date("not-a-date") is None


def test_parse_datetime_handles_z_suffix() -> None:
    assert repo._parse_datetime(None) is None
    assert repo._parse_datetime("") is None
    parsed = repo._parse_datetime("2026-04-10T09:00:00Z")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.month == 4 and parsed.day == 10
    assert repo._parse_datetime("garbage") is None


def test_parse_amount_accepts_strings_and_decimals() -> None:
    assert repo._parse_amount("125.50") == Decimal("125.50")
    assert repo._parse_amount("-42.00") == Decimal("-42.00")
    assert repo._parse_amount(Decimal("99.99")) == Decimal("99.99")
