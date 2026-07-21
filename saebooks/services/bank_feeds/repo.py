"""Persistence layer for the bank-feeds module.

Thin, boring DB helpers for upserting ``BankFeedClient`` / ``BankFeedAccount``
rows from upstream JSON and for batch-inserting ``BankStatementLine`` rows
with idempotent deduplication.

Inputs are treated as plain dicts (typed ``Mapping[str, Any]``) rather than
Pydantic schema objects. The canonical field names match CDR-Banking
standard (e.g. ``accountId``, ``postingDateTime``, ``amount``) — these are
open-standard terms, safe for the public repo.

Dedup contract:
    A ``BankStatementLine`` is uniquely identified by the partial unique
    index ``(bank_feed_account_id, external_id)`` (see migration 0016).
    ``upsert_statement_lines`` therefore uses an ON CONFLICT DO NOTHING —
    we never overwrite a reconciled line. If upstream emits an updated
    version of a txn (amount/description changed) the caller is
    responsible for detecting that and handling it separately.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import upsert_stmt
from saebooks.models.bank_feed import (
    BankFeedAccount,
    BankFeedClient,
    BankFeedIssue,
    BankFeedIssueStatus,
)
from saebooks.services.statement_lines_bulk import (
    BulkLineInput,
    DedupStrategy,
    bulk_create_statement_lines,
)


async def get_or_create_client(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    sds_client_id: str,
) -> BankFeedClient:
    """Return the ``BankFeedClient`` row for a company, creating if absent.

    One company → one aggregator client. If a row already exists for the
    company but the upstream ``sds_client_id`` has changed, raise —
    re-linking a company to a different aggregator client is a manual
    operation that should go through the admin UI (not this silent path).
    """
    existing = await session.execute(
        select(BankFeedClient).where(BankFeedClient.company_id == company_id)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        if row.sds_client_id != sds_client_id:
            raise ValueError(
                f"Company {company_id} already linked to "
                f"{row.sds_client_id}, refusing to re-link to {sds_client_id}"
            )
        return row
    row = BankFeedClient(company_id=company_id, sds_client_id=sds_client_id)
    session.add(row)
    await session.flush()
    return row


async def upsert_bank_feed_account(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    bank_feed_client_id: uuid.UUID,
    ledger_account_id: uuid.UUID,
    account: Mapping[str, Any],
) -> BankFeedAccount:
    """Upsert a ``BankFeedAccount`` from an upstream account payload.

    ``account`` is the raw CDR-Banking account object plus the aggregator's
    ``sds`` extension block. We extract the stable fields and upsert on
    ``sds_account_id``. Caller has already decided which ledger account
    this bank account maps to.
    """
    sds_ext: Mapping[str, Any] = account.get("sds") or {}
    values: dict[str, Any] = {
        "company_id": company_id,
        "bank_feed_client_id": bank_feed_client_id,
        "ledger_account_id": ledger_account_id,
        "sds_account_id": account["accountId"],
        "sds_institution_id": sds_ext.get("sdsInstitutionId") or "",
        "masked_number": account.get("maskedNumber"),
        "display_name": account.get("displayName") or account.get("nickname"),
        "product_category": account.get("productCategory"),
        "feed_type": sds_ext.get("feedType"),
        "processing_status": sds_ext.get("processingStatus"),
        "processing_status_date": _parse_date(sds_ext.get("processingStatusDate")),
        "last_transaction_posted_id": sds_ext.get("lastTransactionPostedId"),
        "last_transaction_posted_date": _parse_date(
            sds_ext.get("lastTransactionPostedDate")
        ),
    }

    stmt = upsert_stmt(BankFeedAccount).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BankFeedAccount.sds_account_id],
        set_={
            "masked_number": values["masked_number"],
            "display_name": values["display_name"],
            "product_category": values["product_category"],
            "feed_type": values["feed_type"],
            "processing_status": values["processing_status"],
            "processing_status_date": values["processing_status_date"],
            "last_transaction_posted_id": values["last_transaction_posted_id"],
            "last_transaction_posted_date": values["last_transaction_posted_date"],
        },
    )
    await session.execute(stmt)
    await session.flush()

    # ``populate_existing`` forces the ORM to refresh any identity-map entry
    # with the newly-written row — without it, a second call in the same
    # session would hand back the stale pre-upsert object.
    result = await session.execute(
        select(BankFeedAccount)
        .where(BankFeedAccount.sds_account_id == values["sds_account_id"])
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def update_sync_cursor(
    session: AsyncSession,
    *,
    bank_feed_account_id: uuid.UUID,
    last_transaction_posted_id: str,
    last_transaction_posted_date: date | None,
) -> None:
    """Persist the canonical sync cursor after a successful transaction fetch."""
    row = await session.get(BankFeedAccount, bank_feed_account_id)
    if row is None:
        raise ValueError(f"BankFeedAccount {bank_feed_account_id} not found")
    row.last_transaction_posted_id = last_transaction_posted_id
    if last_transaction_posted_date is not None:
        row.last_transaction_posted_date = last_transaction_posted_date
    await session.flush()


async def insert_statement_lines(
    session: AsyncSession,
    *,
    bank_feed_account_id: uuid.UUID,
    transactions: Sequence[Mapping[str, Any]],
) -> int:
    """Batch-insert transactions as ``BankStatementLine`` rows.

    Returns the number of *new* rows inserted. Existing rows (matched by
    ``(bank_feed_account_id, external_id)``) are silently skipped.

    CDR-Banking field parsing (``transactionId`` / ``postingDateTime`` /
    ``amount`` / …) stays here; the INSERT + ON CONFLICT DO NOTHING dedup
    is delegated to the shared EXTERNAL_ID bulk-create so the semantics
    are identical to the ``/bank_statement_lines/bulk`` fact API. Passing
    ``tenant_id=None`` keeps the model ``server_default`` (default tenant)
    behaviour byte-for-byte identical to the pre-refactor writer.
    """
    if not transactions:
        return 0

    account = await session.get(BankFeedAccount, bank_feed_account_id)
    if account is None:
        raise ValueError(f"BankFeedAccount {bank_feed_account_id} not found")

    lines = [
        BulkLineInput(
            txn_date=_parse_date(
                txn.get("postingDateTime") or txn.get("executionDateTime")
            ),
            amount=_parse_amount(txn["amount"]),
            description=txn.get("description"),
            reference=txn.get("reference"),
            external_id=txn["transactionId"],
        )
        for txn in transactions
    ]

    result = await bulk_create_statement_lines(
        session,
        company_id=account.company_id,
        account_id=account.ledger_account_id,
        bank_feed_account_id=bank_feed_account_id,
        lines=lines,
        strategy=DedupStrategy.EXTERNAL_ID,
        tenant_id=None,
    )
    return result.created


async def upsert_feed_issue(
    session: AsyncSession,
    *,
    issue: Mapping[str, Any],
) -> BankFeedIssue:
    """Upsert a ``BankFeedIssue`` from an upstream feed-issue payload."""
    status_str = str(issue.get("status", "")).lower()
    status = (
        BankFeedIssueStatus.ACTIVE
        if status_str == "active"
        else BankFeedIssueStatus.CLOSED
    )
    values: dict[str, Any] = {
        "sds_feed_issue_id": issue["feedIssueId"],
        "sds_institution_id": issue["sdsInstitutionId"],
        "status": status.value,
        "creation_datetime": _parse_datetime(issue["creationDateTime"]),
        "closed_datetime": _parse_datetime(issue.get("closedDateTime")),
        "last_message": issue.get("lastMessage"),
        "last_update_datetime": _parse_datetime(issue.get("lastUpdateDateTime")),
        "country": issue.get("country"),
        "fetched_at": datetime.now(),
    }

    stmt = upsert_stmt(BankFeedIssue).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BankFeedIssue.sds_feed_issue_id],
        set_={
            "status": values["status"],
            "closed_datetime": values["closed_datetime"],
            "last_message": values["last_message"],
            "last_update_datetime": values["last_update_datetime"],
            "fetched_at": values["fetched_at"],
        },
    )
    await session.execute(stmt)
    await session.flush()

    result = await session.execute(
        select(BankFeedIssue)
        .where(BankFeedIssue.sds_feed_issue_id == values["sds_feed_issue_id"])
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


# ---------------------------------------------------------------------- #
# Parsers                                                                #
# ---------------------------------------------------------------------- #


def _parse_date(value: Any) -> date | None:
    """Parse a date or date-time string into a ``date``.

    Tolerates both ``YYYY-MM-DD`` and ISO 8601 timestamps; returns ``None``
    on empty/missing input.
    """
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value)
    # Python's fromisoformat handles offsets natively from 3.11+
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp; tolerant of trailing ``Z``."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_amount(value: Any) -> Decimal:
    """Parse an amount string into a ``Decimal``.

    The aggregator emits amounts as strings (CDR ``AmountString``). We
    coerce through ``Decimal`` for lossless persistence into
    ``Numeric(14, 2)``.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
