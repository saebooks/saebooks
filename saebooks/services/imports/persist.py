"""Persist parsed bank statement lines into ``BankStatementLine``.

The parsers (``bank_csv``/``bank_ofx``) return pure ``ParsedLine``
dataclasses. This module is the only place that actually writes rows,
so the router can call the parsers freely during a preview flow without
touching the DB.

Idempotency strategy — each inserted row gets a deterministic
``external_id`` computed from ``(account_id, txn_date, amount,
description, reference_or_fitid)``. Re-importing the same CSV
produces the same hashes, and the partial unique index on
``(bank_feed_account_id, external_id)`` would block the dup —
but CSV imports have no ``bank_feed_account_id`` so we do the
dedup ourselves in Python against the existing hashes for that
account before INSERT. Keeps the query count flat regardless of
file size.
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.company import Company
from saebooks.services.imports.bank_csv import ParsedLine

# Stamp-prefix so the hash namespace can't collide with upstream
# feed transaction IDs (those are upstream GUIDs).
_IMPORT_PREFIX = "csv:"


def _fingerprint(
    account_id: uuid.UUID, line: ParsedLine
) -> str:
    """Stable hash of the identifying fields.

    Reference/FITID is included when present so OFX duplicates across
    different statement windows stay idempotent even if description
    drifts (banks sometimes reformat memos between exports).
    """
    parts = (
        str(account_id),
        line.txn_date.isoformat(),
        f"{line.amount:.2f}",
        line.description or "",
        line.reference or "",
    )
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return _IMPORT_PREFIX + h


async def persist_bank_lines(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    lines: Sequence[ParsedLine],
) -> int:
    """Insert ``lines`` as ``BankStatementLine`` rows; return new-row count.

    Duplicate rows (same fingerprint, same account) are silently
    skipped. Commit is left to the caller.
    """
    if not lines:
        return 0

    # bank_statement_lines has FORCE RLS with a tenant_isolation policy
    # — INSERTs are rejected unless tenant_id matches
    # current_setting('app.current_tenant'). The model defaults to the
    # placeholder UUID, so we have to look up the company's real tenant
    # and stamp it explicitly. Otherwise CSV imports 500 with
    # InsufficientPrivilegeError. (Reported 2026-05-23 importing Sauer
    # Pty Ltd Business One.)
    tenant_id = (
        await session.execute(select(Company.tenant_id).where(Company.id == company_id))
    ).scalar_one_or_none()
    if tenant_id is None:
        raise ValueError(f"Company {company_id} not found — cannot resolve tenant for bank import")

    fingerprints = [_fingerprint(account_id, ln) for ln in lines]

    existing = (
        await session.execute(
            select(BankStatementLine.external_id).where(
                BankStatementLine.account_id == account_id,
                BankStatementLine.external_id.in_(fingerprints),
            )
        )
    ).scalars().all()
    existing_set = set(existing)

    new_rows = []
    for fp, parsed in zip(fingerprints, lines, strict=True):
        if fp in existing_set:
            continue
        existing_set.add(fp)  # guard against intra-batch dups
        new_rows.append(
            BankStatementLine(
                company_id=company_id,
                tenant_id=tenant_id,
                account_id=account_id,
                txn_date=parsed.txn_date,
                amount=parsed.amount,
                description=parsed.description,
                reference=parsed.reference,
                external_id=fp,
                status=StatementLineStatus.UNMATCHED,
            )
        )

    if not new_rows:
        return 0
    session.add_all(new_rows)
    await session.flush()
    return len(new_rows)


__all__ = ["persist_bank_lines"]
