"""Sequential, gap-free, per-company document numbering.

Use ``next_number`` whenever a user-visible document number needs to
be minted — invoices, bills, credit notes, payments, quotes. Each
kind has its own counter scoped to the company, so INV-000042 and
BILL-000007 advance independently.

Atomicity:
    The advance is a ``SELECT ... FOR UPDATE`` on the counter row,
    followed by a write. Two concurrent requests serialise on the row
    lock — neither can skip or duplicate a number even under heavy
    load. Gap-free numbering is an AU ATO tax-invoice requirement.

Defaults:
    The first call for a (company, kind) that has no counter row
    creates one using the defaults below; callers can override
    ``prefix`` and ``pad_width`` on that first call only. Later calls
    ignore those kwargs — the counter is already materialised.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.document_counter import DocumentCounter

KNOWN_KINDS = frozenset(
    {"invoice", "bill", "credit_note", "payment", "quote", "statement", "fixed_asset"}
)

# Default prefix / pad per kind. Override the prefix via ``next_number``
# only on first use (when the counter row does not yet exist); later
# calls re-use the prefix that landed in the row. Keep the pad at 6 —
# enough for 999,999 documents per counter before a human ever sees it
# wrap to 7 digits.
_DEFAULTS: dict[str, tuple[str, int]] = {
    "invoice": ("INV-", 6),
    "bill": ("BILL-", 6),
    "credit_note": ("CN-", 6),
    "payment": ("PAY-", 6),
    "quote": ("Q-", 6),
    "statement": ("STMT-", 6),
    "fixed_asset": ("AST-", 6),
}


async def next_number(
    session: AsyncSession,
    company_id: uuid.UUID,
    kind: str,
    *,
    prefix: str | None = None,
    pad_width: int | None = None,
) -> str:
    """Return the next document number for (company, kind), advancing the counter.

    Caller is responsible for COMMITting the surrounding transaction —
    we only flush here so the counter row lock is taken in-scope with
    the caller's write. If the caller rolls back, the counter rolls
    back with it: numbering stays gap-free.
    """
    if kind not in KNOWN_KINDS:
        raise ValueError(
            f"Unknown document kind {kind!r}; known kinds: {sorted(KNOWN_KINDS)}"
        )

    # SELECT ... FOR UPDATE locks the row for the rest of the txn.
    stmt = (
        select(DocumentCounter)
        .where(
            DocumentCounter.company_id == company_id,
            DocumentCounter.kind == kind,
        )
        .with_for_update()
    )
    result = await session.execute(stmt)
    counter = result.scalar_one_or_none()

    if counter is None:
        default_prefix, default_pad = _DEFAULTS.get(kind, ("", 6))
        counter = DocumentCounter(
            company_id=company_id,
            kind=kind,
            prefix=prefix if prefix is not None else default_prefix,
            pad_width=pad_width if pad_width is not None else default_pad,
            next_value=1,
        )
        session.add(counter)
        await session.flush()

    value = counter.next_value
    counter.next_value = value + 1
    await session.flush()

    return f"{counter.prefix}{value:0{counter.pad_width}d}"


async def peek_next(
    session: AsyncSession,
    company_id: uuid.UUID,
    kind: str,
) -> str:
    """Return what the next number would be without advancing. For UI preview."""
    if kind not in KNOWN_KINDS:
        raise ValueError(f"Unknown document kind {kind!r}")

    result = await session.execute(
        select(DocumentCounter).where(
            DocumentCounter.company_id == company_id,
            DocumentCounter.kind == kind,
        )
    )
    counter = result.scalar_one_or_none()
    if counter is None:
        default_prefix, default_pad = _DEFAULTS.get(kind, ("", 6))
        return f"{default_prefix}{1:0{default_pad}d}"
    return f"{counter.prefix}{counter.next_value:0{counter.pad_width}d}"
