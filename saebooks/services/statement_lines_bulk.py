"""Shared bulk ``BankStatementLine`` create with dedup.

ONE implementation sits behind BOTH in-process writers *and* the
``POST /api/v1/bank_statement_lines/bulk`` fact API, so their dedup
semantics can never drift (capture-module extraction step 1, gitea #32):

* ``services/imports/persist.py::persist_bank_lines`` (CSV / OFX) uses the
  **fingerprint** strategy — a deterministic ``external_id`` hashed from
  ``(account_id, txn_date, amount, description, reference)`` plus an
  **intra-batch occurrence index** so genuinely-distinct rows that share a
  base fingerprint (Westpac CSV carries no per-line reference) survive,
  while re-importing the same file yields zero new rows.
* ``services/bank_feeds/repo.py::insert_statement_lines`` (aggregator feed)
  uses the **external_id** strategy — an upstream ``transactionId`` deduped
  by ``ON CONFLICT DO NOTHING`` on the partial unique index
  ``(bank_feed_account_id, external_id) WHERE external_id IS NOT NULL``
  (migration 0016).

The two strategies are deliberately preserved verbatim; the only shared
code is the row-building and the dedup mechanics, extracted here so the
endpoint runs the exact same code the writers do.

Tenant / RLS note
-----------------
``bank_statement_lines`` has FORCE RLS with a ``tenant_isolation`` policy —
an INSERT is rejected unless ``tenant_id`` matches
``current_setting('app.current_tenant')``.

* FINGERPRINT resolves the company's real tenant and stamps it explicitly
  (the model default is a placeholder that RLS would reject) — this mirrors
  ``persist_bank_lines`` exactly.
* EXTERNAL_ID leaves ``tenant_id`` unset unless the caller passes one, so
  the model ``server_default`` applies — this mirrors ``insert_statement_lines``
  exactly (the feed path runs in the default tenant today). The bulk
  endpoint passes the request tenant so the fact API is correct for any
  tenant; the in-process feed writer passes ``None`` to stay byte-for-byte
  identical to the pre-refactor behaviour.

Commit is always left to the caller (both writers ``flush`` only).
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import upsert_stmt
from saebooks.models.bank_statement import BankStatementLine, StatementLineStatus
from saebooks.models.company import Company

# Stamp-prefix so the hash namespace can't collide with upstream feed
# transaction IDs (those are upstream GUIDs).
_IMPORT_PREFIX = "csv:"


class DedupStrategy(StrEnum):
    """Which dedup mechanism the bulk create uses."""

    #: Hash-based external_id + intra-batch occurrence index (CSV / OFX).
    FINGERPRINT = "fingerprint"
    #: Upstream external_id + ON CONFLICT DO NOTHING (aggregator feed).
    EXTERNAL_ID = "external_id"


@dataclass(frozen=True)
class BulkLineInput:
    """One statement line ready for bulk persistence.

    ``amount`` is signed (positive = deposit, negative = withdrawal). For
    the EXTERNAL_ID strategy ``external_id`` MUST be set (the upstream
    ``transactionId``); for FINGERPRINT it is ignored and computed.
    """

    txn_date: date
    amount: Decimal
    description: str | None = None
    reference: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class BulkResult:
    """Outcome of a bulk create: created vs deduped counts + new ids."""

    created: int
    deduped: int
    ids: list[uuid.UUID]


def _fingerprint(
    account_id: uuid.UUID,
    txn_date: date,
    amount: Decimal,
    description: str | None,
    reference: str | None,
) -> str:
    """Stable hash of the identifying fields.

    Reference is included when present so OFX duplicates across different
    statement windows stay idempotent even if description drifts (banks
    sometimes reformat memos between exports).
    """
    parts = (
        str(account_id),
        txn_date.isoformat(),
        f"{amount:.2f}",
        description or "",
        reference or "",
    )
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return _IMPORT_PREFIX + h


async def _resolve_tenant_id(
    session: AsyncSession, company_id: uuid.UUID
) -> uuid.UUID:
    tenant_id = (
        await session.execute(
            select(Company.tenant_id).where(Company.id == company_id)
        )
    ).scalar_one_or_none()
    if tenant_id is None:
        raise ValueError(
            f"Company {company_id} not found — cannot resolve tenant for "
            "bank statement import"
        )
    return tenant_id


async def bulk_create_statement_lines(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    lines: Sequence[BulkLineInput],
    strategy: DedupStrategy,
    bank_feed_account_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> BulkResult:
    """Bulk-insert ``lines`` as ``BankStatementLine`` rows, deduped.

    Returns ``BulkResult(created, deduped, ids)`` where ``deduped`` is the
    number of input lines that already existed (``created + deduped ==
    len(lines)``). Commit is left to the caller.
    """
    if not lines:
        return BulkResult(created=0, deduped=0, ids=[])

    if strategy == DedupStrategy.FINGERPRINT:
        if tenant_id is None:
            tenant_id = await _resolve_tenant_id(session, company_id)
        return await _fingerprint_bulk(
            session,
            company_id=company_id,
            account_id=account_id,
            tenant_id=tenant_id,
            lines=lines,
        )
    if strategy == DedupStrategy.EXTERNAL_ID:
        if bank_feed_account_id is None:
            raise ValueError(
                "EXTERNAL_ID strategy requires bank_feed_account_id"
            )
        return await _external_id_bulk(
            session,
            company_id=company_id,
            account_id=account_id,
            bank_feed_account_id=bank_feed_account_id,
            tenant_id=tenant_id,
            lines=lines,
        )
    raise ValueError(f"Unknown dedup strategy: {strategy!r}")


async def _fingerprint_bulk(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    lines: Sequence[BulkLineInput],
) -> BulkResult:
    """Fingerprint dedup — reproduces ``persist_bank_lines`` exactly.

    Intra-batch occurrence index: genuinely-distinct rows that share a base
    fingerprint (no per-line reference) hash the same, so the Nth occurrence
    of a base fingerprint WITHIN this batch is distinguished — the 1st keeps
    the bare fingerprint (backward compatible), the 2nd gets ``:n2``, the
    3rd ``:n3`` and so on. Re-importing the same file replays the same
    occurrence sequence -> the same external_ids -> all already exist ->
    zero new rows (idempotent). Distinct collisions get distinct ids -> all
    preserved.
    """
    seen_counts: dict[str, int] = {}
    candidate_ids: list[str] = []
    for ln in lines:
        base = _fingerprint(
            account_id, ln.txn_date, ln.amount, ln.description, ln.reference
        )
        n = seen_counts.get(base, 0) + 1
        seen_counts[base] = n
        candidate_ids.append(base if n == 1 else f"{base}:n{n}")

    existing = (
        await session.execute(
            select(BankStatementLine.external_id).where(
                BankStatementLine.account_id == account_id,
                BankStatementLine.external_id.in_(candidate_ids),
            )
        )
    ).scalars().all()
    existing_set = set(existing)

    new_rows: list[BankStatementLine] = []
    for external_id, parsed in zip(candidate_ids, lines, strict=True):
        # Across-batch existence check (re-import of the same file). The
        # occurrence index already disambiguates within this batch, so we do
        # NOT re-add to existing_set here.
        if external_id in existing_set:
            continue
        new_rows.append(
            BankStatementLine(
                company_id=company_id,
                tenant_id=tenant_id,
                account_id=account_id,
                txn_date=parsed.txn_date,
                amount=parsed.amount,
                description=parsed.description,
                reference=parsed.reference,
                external_id=external_id,
                status=StatementLineStatus.UNMATCHED,
            )
        )

    if not new_rows:
        return BulkResult(created=0, deduped=len(lines), ids=[])
    session.add_all(new_rows)
    await session.flush()
    ids = [row.id for row in new_rows]
    return BulkResult(
        created=len(new_rows),
        deduped=len(lines) - len(new_rows),
        ids=ids,
    )


async def _external_id_bulk(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    account_id: uuid.UUID,
    bank_feed_account_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    lines: Sequence[BulkLineInput],
) -> BulkResult:
    """external_id dedup — reproduces ``insert_statement_lines`` exactly.

    ON CONFLICT DO NOTHING against the partial unique index created in
    migration 0016. Postgres needs the partial ``WHERE`` predicate repeated
    here so it can match the correct index. ``RETURNING id`` lets us count
    (and return) the newly-inserted rows.
    """
    rows: list[dict[str, object]] = []
    for ln in lines:
        row: dict[str, object] = {
            "company_id": company_id,
            "account_id": account_id,
            "bank_feed_account_id": bank_feed_account_id,
            "external_id": ln.external_id,
            "txn_date": ln.txn_date,
            "description": ln.description,
            "amount": ln.amount,
            "reference": ln.reference,
            "status": StatementLineStatus.UNMATCHED.value,
        }
        # Only stamp tenant_id when the caller supplied it; otherwise the
        # model server_default applies (byte-for-byte identical to the
        # pre-refactor feed writer, which ran in the default tenant).
        if tenant_id is not None:
            row["tenant_id"] = tenant_id
        rows.append(row)

    stmt = upsert_stmt(BankStatementLine).values(rows)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["bank_feed_account_id", "external_id"],
        index_where=text("external_id IS NOT NULL"),
    )
    result = await session.execute(stmt.returning(BankStatementLine.id))
    inserted_ids = list(result.scalars().all())
    await session.flush()
    return BulkResult(
        created=len(inserted_ids),
        deduped=len(lines) - len(inserted_ids),
        ids=inserted_ids,
    )


__all__ = [
    "BulkLineInput",
    "BulkResult",
    "DedupStrategy",
    "bulk_create_statement_lines",
]
