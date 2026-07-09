"""Persist parsed bank statement lines into ``BankStatementLine``.

The parsers (``bank_csv``/``bank_ofx``) return pure ``ParsedLine``
dataclasses. This module is the thin CSV/OFX adapter over the shared
bulk-create service (``services.statement_lines_bulk``) — the same
implementation that backs ``POST /api/v1/bank_statement_lines/bulk`` and
the bank-feeds feed writer, so their dedup semantics can never drift
(capture-module extraction step 1, gitea #32).

Idempotency strategy — the FINGERPRINT dedup: each inserted row gets a
deterministic ``external_id`` hashed from ``(account_id, txn_date,
amount, description, reference)``, plus an intra-batch occurrence index
so genuinely-distinct rows sharing a base fingerprint survive while a
re-imported file yields zero new rows. The full mechanics (and the
tenant/RLS stamping this CSV path needs) live in
``services.statement_lines_bulk``.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.services.imports.bank_csv import ParsedLine
from saebooks.services.statement_lines_bulk import (
    BulkLineInput,
    DedupStrategy,
    bulk_create_statement_lines,
)


async def persist_bank_lines(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    lines: Sequence[ParsedLine],
) -> int:
    """Insert ``lines`` as ``BankStatementLine`` rows; return new-row count.

    Duplicate rows (same fingerprint, same account) are silently
    skipped. Commit is left to the caller. Delegates to the shared
    FINGERPRINT bulk-create so the semantics are identical to the
    ``/bank_statement_lines/bulk`` fact API.
    """
    if not lines:
        return 0

    result = await bulk_create_statement_lines(
        session,
        company_id=company_id,
        account_id=account_id,
        lines=[
            BulkLineInput(
                txn_date=ln.txn_date,
                amount=ln.amount,
                description=ln.description,
                reference=ln.reference,
            )
            for ln in lines
        ],
        strategy=DedupStrategy.FINGERPRINT,
    )
    return result.created


__all__ = ["persist_bank_lines"]
