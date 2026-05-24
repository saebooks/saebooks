"""Tests for saebooks.services.bank_feeds.reconcile.

Covers the weekly reconciliation sweep:

- Pure helpers (``stale_cutoff``, ``_fmt_report_line``, severity rollup).
- End-to-end sweep shape against a scratch company + ledger account:
  healthy / variance / stale / unmatched paths.
- Revoked feeds are ignored.
- ``sweep_all_companies`` covers every company with at least one active feed.
- Per-account cleanup is aggressive so repeated runs against the persistent
  dev DB never leave orphan bank_feed_accounts/statement_lines behind.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.bank_feed import BankFeedAccount, BankFeedClient
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.services.bank_feeds import reconcile
import pytest
pytestmark = pytest.mark.postgres_only

# ---------------------------------------------------------------------- #
# Test fixtures — scratch company + scratch ledger account + scratch     #
# feed client/account. Each test owns its own fresh Company so we don't  #
# collide with the seeded company's bank_feed_clients (unique on         #
# company_id) or other concurrently-running tests.                       #
# ---------------------------------------------------------------------- #


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _mk_company() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = Company(name=f"ReconTest-{_tag()}")
        session.add(company)
        await session.flush()
        cid = company.id
        await session.commit()
    return cid


async def _mk_ledger(company_id: uuid.UUID) -> uuid.UUID:
    """Make a scratch ASSET ledger for this test — unique code per call."""
    async with AsyncSessionLocal() as session:
        acct = Account(
            company_id=company_id,
            code=f"9-{_tag()[:5]}",
            name="Scratch Bank",
            account_type=AccountType.ASSET,
            reconcile=True,
        )
        session.add(acct)
        await session.flush()
        aid = acct.id
        await session.commit()
    return aid


async def _mk_feed(
    company_id: uuid.UUID, ledger_id: uuid.UUID, *, created_at: datetime | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a BankFeedClient + BankFeedAccount; return (client_id, account_id)."""
    async with AsyncSessionLocal() as session:
        client = BankFeedClient(
            company_id=company_id,
            sds_client_id=f"sds-client-{_tag()}",
        )
        session.add(client)
        await session.flush()

        acct = BankFeedAccount(
            company_id=company_id,
            bank_feed_client_id=client.id,
            ledger_account_id=ledger_id,
            sds_account_id=f"sds-acct-{_tag()}",
            sds_institution_id="INST1",
            display_name="Scratch Feed",
            masked_number="xxxxx1234",
        )
        session.add(acct)
        await session.flush()
        if created_at is not None:
            acct.created_at = created_at
            await session.flush()
        aid = acct.id
        cid = client.id
        await session.commit()
    return cid, aid


async def _add_statement_line(
    company_id: uuid.UUID,
    ledger_id: uuid.UUID,
    feed_id: uuid.UUID,
    *,
    txn_date: date,
    amount: Decimal,
    status: StatementLineStatus = StatementLineStatus.MATCHED,
) -> None:
    async with AsyncSessionLocal() as session:
        line = BankStatementLine(
            company_id=company_id,
            account_id=ledger_id,
            bank_feed_account_id=feed_id,
            txn_date=txn_date,
            amount=amount,
            description="test",
            external_id=f"txn-{_tag()}",
            status=status.value,
        )
        session.add(line)
        await session.commit()


async def _post_journal(
    company_id: uuid.UUID,
    ledger_id: uuid.UUID,
    counter_id: uuid.UUID,
    *,
    entry_date: date,
    debit: Decimal = Decimal("0"),
    credit: Decimal = Decimal("0"),
) -> None:
    """Post a balanced 2-line journal that moves debit-credit on ledger_id."""
    async with AsyncSessionLocal() as session:
        entry = JournalEntry(
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            ref=f"REC-{_tag()}",
            entry_date=entry_date,
            description="Reconcile test",
            status=EntryStatus.POSTED,
            posted_at=datetime.now(UTC),
            posted_by="test",
        )
        session.add(entry)
        await session.flush()
        # If debit>0 on ledger, credit counter; if credit>0 on ledger, debit counter.
        if debit > 0:
            session.add(
                JournalLine(
                    entry_id=entry.id, line_no=1, account_id=ledger_id,
                    debit=debit, credit=Decimal("0"),
                )
            )
            session.add(
                JournalLine(
                    entry_id=entry.id, line_no=2, account_id=counter_id,
                    debit=Decimal("0"), credit=debit,
                )
            )
        else:
            session.add(
                JournalLine(
                    entry_id=entry.id, line_no=1, account_id=ledger_id,
                    debit=Decimal("0"), credit=credit,
                )
            )
            session.add(
                JournalLine(
                    entry_id=entry.id, line_no=2, account_id=counter_id,
                    debit=credit, credit=Decimal("0"),
                )
            )
        await session.commit()


async def _cleanup(company_id: uuid.UUID) -> None:
    """Aggressive teardown — wipe every row under this scratch company."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(BankStatementLine).where(
                BankStatementLine.company_id == company_id
            )
        )
        await session.execute(
            delete(JournalLine).where(
                JournalLine.entry_id.in_(
                    select(JournalEntry.id).where(
                        JournalEntry.company_id == company_id
                    )
                )
            )
        )
        await session.execute(
            delete(JournalEntry).where(JournalEntry.company_id == company_id)
        )
        await session.execute(
            delete(BankFeedAccount).where(
                BankFeedAccount.company_id == company_id
            )
        )
        await session.execute(
            delete(BankFeedClient).where(BankFeedClient.company_id == company_id)
        )
        await session.execute(
            delete(Account).where(Account.company_id == company_id)
        )
        await session.execute(
            delete(Company).where(Company.id == company_id)
        )
        await session.commit()


# ---------------------------------------------------------------------- #
# Pure helpers                                                           #
# ---------------------------------------------------------------------- #


def test_stale_cutoff_default_is_seven_days() -> None:
    assert reconcile.stale_cutoff(date(2026, 4, 20)) == date(2026, 4, 13)


def test_stale_cutoff_respects_override() -> None:
    assert reconcile.stale_cutoff(date(2026, 4, 20), stale_days=14) == date(
        2026, 4, 6
    )


def test_fmt_report_line_includes_key_fields() -> None:
    a = reconcile.AccountHealth(
        bank_feed_account_id=uuid.uuid4(),
        ledger_account_id=uuid.uuid4(),
        ledger_account_code="1-1110",
        ledger_account_name="Bank",
        display_name="Everyday",
        masked_number="xxxxx1234",
        last_statement_date=date(2026, 4, 10),
        days_since_last_statement=3,
        stale=False,
        unmatched_count=0,
        feed_total=Decimal("100.00"),
        gl_total=Decimal("100.00"),
        variance=Decimal("0.00"),
        has_variance=False,
    )
    line = reconcile._fmt_report_line(a)
    assert "account=1-1110" in line
    assert "feed_total=100.00" in line
    assert "gl_total=100.00" in line
    assert "variance=0.00" in line
    assert "severity=ok" in line
    assert "last_txn=2026-04-10" in line


def test_severity_ok_when_clean() -> None:
    a = _mk_health(stale=False, unmatched=0, variance="0.00")
    assert a.severity == "ok"


def test_severity_warn_when_unmatched_but_balanced() -> None:
    a = _mk_health(stale=False, unmatched=3, variance="0.00")
    assert a.severity == "warn"


def test_severity_error_when_stale() -> None:
    a = _mk_health(stale=True, unmatched=0, variance="0.00")
    assert a.severity == "error"


def test_severity_error_when_variance() -> None:
    a = _mk_health(stale=False, unmatched=0, variance="5.00", has_variance=True)
    assert a.severity == "error"


def _mk_health(
    *,
    stale: bool,
    unmatched: int,
    variance: str,
    has_variance: bool | None = None,
) -> reconcile.AccountHealth:
    return reconcile.AccountHealth(
        bank_feed_account_id=uuid.uuid4(),
        ledger_account_id=uuid.uuid4(),
        ledger_account_code="1-1110",
        ledger_account_name="Bank",
        display_name=None,
        masked_number=None,
        last_statement_date=date(2026, 4, 10),
        days_since_last_statement=3,
        stale=stale,
        unmatched_count=unmatched,
        feed_total=Decimal("0.00"),
        gl_total=Decimal("0.00"),
        variance=Decimal(variance),
        has_variance=bool(has_variance) if has_variance is not None else (Decimal(variance) != 0),
    )


def test_worst_severity_rollup() -> None:
    report = reconcile.ReconciliationReport(
        company_id=uuid.uuid4(),
        through_date=date(2026, 4, 20),
        accounts=[
            _mk_health(stale=False, unmatched=0, variance="0.00"),
            _mk_health(stale=False, unmatched=2, variance="0.00"),
        ],
    )
    assert report.worst_severity == "warn"
    assert report.has_any_issue is True

    report_error = reconcile.ReconciliationReport(
        company_id=uuid.uuid4(),
        through_date=date(2026, 4, 20),
        accounts=[
            _mk_health(stale=False, unmatched=2, variance="0.00"),
            _mk_health(stale=True, unmatched=0, variance="0.00"),
        ],
    )
    assert report_error.worst_severity == "error"


def test_worst_severity_ok_when_empty() -> None:
    report = reconcile.ReconciliationReport(
        company_id=uuid.uuid4(),
        through_date=date(2026, 4, 20),
        accounts=[],
    )
    assert report.worst_severity == "ok"
    assert report.total_variance == Decimal("0")
    assert report.has_any_issue is False


# ---------------------------------------------------------------------- #
# DB-backed sweep paths                                                  #
# ---------------------------------------------------------------------- #


async def test_sweep_empty_when_no_feeds() -> None:
    company_id = await _mk_company()
    try:
        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session, company_id=company_id, through_date=date(2026, 4, 20)
            )
        assert report.accounts == []
        assert report.total_variance == Decimal("0")
        assert report.worst_severity == "ok"
    finally:
        await _cleanup(company_id)


async def test_sweep_healthy_account() -> None:
    company_id = await _mk_company()
    try:
        ledger = await _mk_ledger(company_id)
        counter = await _mk_ledger(company_id)  # counterparty for balanced entries
        _client, feed = await _mk_feed(company_id, ledger)

        through = date(2026, 4, 20)
        await _add_statement_line(
            company_id, ledger, feed,
            txn_date=date(2026, 4, 18), amount=Decimal("125.50"),
        )
        await _post_journal(
            company_id, ledger, counter,
            entry_date=date(2026, 4, 18), debit=Decimal("125.50"),
        )

        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session, company_id=company_id, through_date=through
            )

        assert len(report.accounts) == 1
        a = report.accounts[0]
        assert a.feed_total == Decimal("125.50")
        assert a.gl_total == Decimal("125.50")
        assert a.variance == Decimal("0.00")
        assert a.has_variance is False
        assert a.stale is False
        assert a.unmatched_count == 0
        assert a.severity == "ok"
        assert a.last_statement_date == date(2026, 4, 18)
        assert a.days_since_last_statement == 2
    finally:
        await _cleanup(company_id)


async def test_sweep_variance_flags_error() -> None:
    company_id = await _mk_company()
    try:
        ledger = await _mk_ledger(company_id)
        counter = await _mk_ledger(company_id)
        _, feed = await _mk_feed(company_id, ledger)

        # Feed says +100, GL says +90 → variance 10.00 → error.
        await _add_statement_line(
            company_id, ledger, feed,
            txn_date=date(2026, 4, 18), amount=Decimal("100.00"),
        )
        await _post_journal(
            company_id, ledger, counter,
            entry_date=date(2026, 4, 18), debit=Decimal("90.00"),
        )

        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session, company_id=company_id, through_date=date(2026, 4, 20)
            )

        a = report.accounts[0]
        assert a.feed_total == Decimal("100.00")
        assert a.gl_total == Decimal("90.00")
        assert a.variance == Decimal("10.00")
        assert a.has_variance is True
        assert a.severity == "error"
        assert report.total_variance == Decimal("10.00")
    finally:
        await _cleanup(company_id)


async def test_sweep_variance_under_tolerance_is_ok() -> None:
    """Rounding noise (<= $0.01) must not trip the variance flag."""
    company_id = await _mk_company()
    try:
        ledger = await _mk_ledger(company_id)
        counter = await _mk_ledger(company_id)
        _, feed = await _mk_feed(company_id, ledger)

        # Feed 100.01, GL 100.00 — exactly at tolerance; should be clean.
        await _add_statement_line(
            company_id, ledger, feed,
            txn_date=date(2026, 4, 18), amount=Decimal("100.01"),
        )
        await _post_journal(
            company_id, ledger, counter,
            entry_date=date(2026, 4, 18), debit=Decimal("100.00"),
        )

        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session, company_id=company_id, through_date=date(2026, 4, 20)
            )

        a = report.accounts[0]
        assert a.variance == Decimal("0.01")
        assert a.has_variance is False
        assert a.severity == "ok"
    finally:
        await _cleanup(company_id)


async def test_sweep_stale_feed_flags_error() -> None:
    company_id = await _mk_company()
    try:
        ledger = await _mk_ledger(company_id)
        counter = await _mk_ledger(company_id)
        _, feed = await _mk_feed(company_id, ledger)

        # Last txn 20 days ago — stale (default cutoff 7).
        await _add_statement_line(
            company_id, ledger, feed,
            txn_date=date(2026, 4, 1), amount=Decimal("50.00"),
        )
        await _post_journal(
            company_id, ledger, counter,
            entry_date=date(2026, 4, 1), debit=Decimal("50.00"),
        )

        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session, company_id=company_id, through_date=date(2026, 4, 20)
            )

        a = report.accounts[0]
        assert a.stale is True
        assert a.days_since_last_statement == 19
        assert a.severity == "error"
        # Feed + GL balanced, so has_variance is False
        assert a.has_variance is False
    finally:
        await _cleanup(company_id)


async def test_sweep_stale_days_override_tolerates_longer_gap() -> None:
    company_id = await _mk_company()
    try:
        ledger = await _mk_ledger(company_id)
        counter = await _mk_ledger(company_id)
        _, feed = await _mk_feed(company_id, ledger)

        await _add_statement_line(
            company_id, ledger, feed,
            txn_date=date(2026, 4, 1), amount=Decimal("50.00"),
        )
        await _post_journal(
            company_id, ledger, counter,
            entry_date=date(2026, 4, 1), debit=Decimal("50.00"),
        )

        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session,
                company_id=company_id,
                through_date=date(2026, 4, 20),
                stale_days=30,
            )

        assert report.accounts[0].stale is False
        assert report.accounts[0].severity == "ok"
    finally:
        await _cleanup(company_id)


async def test_sweep_unmatched_lines_warn() -> None:
    company_id = await _mk_company()
    try:
        ledger = await _mk_ledger(company_id)
        counter = await _mk_ledger(company_id)
        _, feed = await _mk_feed(company_id, ledger)

        # Balanced totals, but one line is still UNMATCHED.
        await _add_statement_line(
            company_id, ledger, feed,
            txn_date=date(2026, 4, 18), amount=Decimal("60.00"),
            status=StatementLineStatus.MATCHED,
        )
        await _add_statement_line(
            company_id, ledger, feed,
            txn_date=date(2026, 4, 19), amount=Decimal("40.00"),
            status=StatementLineStatus.UNMATCHED,
        )
        await _post_journal(
            company_id, ledger, counter,
            entry_date=date(2026, 4, 19), debit=Decimal("100.00"),
        )

        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session, company_id=company_id, through_date=date(2026, 4, 20)
            )

        a = report.accounts[0]
        assert a.feed_total == Decimal("100.00")
        assert a.gl_total == Decimal("100.00")
        assert a.unmatched_count == 1
        assert a.stale is False
        assert a.has_variance is False
        assert a.severity == "warn"
    finally:
        await _cleanup(company_id)


async def test_sweep_no_statement_lines_new_feed_is_not_stale() -> None:
    """A feed created <7 days ago with no statement lines yet is still ok."""
    company_id = await _mk_company()
    try:
        ledger = await _mk_ledger(company_id)
        fresh_created = datetime.now(UTC) - timedelta(days=2)
        _, feed = await _mk_feed(company_id, ledger, created_at=fresh_created)

        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session, company_id=company_id, through_date=date.today()
            )

        a = report.accounts[0]
        assert a.last_statement_date is None
        assert a.days_since_last_statement is None
        assert a.stale is False
        assert a.severity == "ok"
        # feed existed but no statements — both totals are 0
        assert a.feed_total == Decimal("0.00")
        assert a.gl_total == Decimal("0.00")
        assert a.bank_feed_account_id == feed
    finally:
        await _cleanup(company_id)


async def test_sweep_no_statement_lines_old_feed_is_stale() -> None:
    """A feed older than the stale cutoff with no statements is flagged stale."""
    company_id = await _mk_company()
    try:
        ledger = await _mk_ledger(company_id)
        old_created = datetime.now(UTC) - timedelta(days=30)
        _, _feed = await _mk_feed(company_id, ledger, created_at=old_created)

        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session, company_id=company_id, through_date=date.today()
            )

        a = report.accounts[0]
        assert a.stale is True
        assert a.severity == "error"
    finally:
        await _cleanup(company_id)


async def test_sweep_excludes_revoked_feeds() -> None:
    company_id = await _mk_company()
    try:
        ledger = await _mk_ledger(company_id)
        _, feed = await _mk_feed(company_id, ledger)

        # Revoke it — should drop out of the sweep entirely.
        async with AsyncSessionLocal() as session:
            row = await session.get(BankFeedAccount, feed)
            assert row is not None
            row.revoked_at = datetime.now(UTC)
            await session.commit()

        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session, company_id=company_id, through_date=date(2026, 4, 20)
            )

        assert report.accounts == []
        assert report.worst_severity == "ok"
    finally:
        await _cleanup(company_id)


async def test_sweep_ignores_draft_journal_entries() -> None:
    """Unposted drafts must not contribute to GL total."""
    company_id = await _mk_company()
    try:
        ledger = await _mk_ledger(company_id)
        counter = await _mk_ledger(company_id)
        _, feed = await _mk_feed(company_id, ledger)

        await _add_statement_line(
            company_id, ledger, feed,
            txn_date=date(2026, 4, 18), amount=Decimal("50.00"),
        )
        # Post a balanced 50.00 entry
        await _post_journal(
            company_id, ledger, counter,
            entry_date=date(2026, 4, 18), debit=Decimal("50.00"),
        )
        # Add a DRAFT entry — should NOT affect GL total
        async with AsyncSessionLocal() as session:
            draft = JournalEntry(
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                ref=f"REC-DRAFT-{_tag()}",
                entry_date=date(2026, 4, 19),
                status=EntryStatus.DRAFT,
            )
            session.add(draft)
            await session.flush()
            session.add(
                JournalLine(
                    entry_id=draft.id, line_no=1, account_id=ledger,
                    debit=Decimal("999.00"), credit=Decimal("0"),
                )
            )
            session.add(
                JournalLine(
                    entry_id=draft.id, line_no=2, account_id=counter,
                    debit=Decimal("0"), credit=Decimal("999.00"),
                )
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            report = await reconcile.sweep(
                session, company_id=company_id, through_date=date(2026, 4, 20)
            )

        a = report.accounts[0]
        assert a.gl_total == Decimal("50.00")
        assert a.severity == "ok"
    finally:
        await _cleanup(company_id)


async def test_sweep_all_companies_covers_every_company_with_feeds() -> None:
    company_a = await _mk_company()
    company_b = await _mk_company()
    try:
        ledger_a = await _mk_ledger(company_a)
        _, _ = await _mk_feed(company_a, ledger_a)

        ledger_b = await _mk_ledger(company_b)
        _, _ = await _mk_feed(company_b, ledger_b)

        async with AsyncSessionLocal() as session:
            reports = await reconcile.sweep_all_companies(session)

        covered = {r.company_id for r in reports}
        assert company_a in covered
        assert company_b in covered
    finally:
        await _cleanup(company_a)
        await _cleanup(company_b)
