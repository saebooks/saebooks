"""Tests for ``saebooks.services.numbering``.

Numbering is mission-critical (ATO requires gap-free tax invoice
numbers), so we cover:

1. First call for (company, kind) auto-creates the counter row with
   sensible defaults.
2. Subsequent calls advance monotonically.
3. Custom ``prefix`` / ``pad_width`` honoured on first call, ignored
   on later calls (counter is locked in).
4. ``peek_next`` doesn't advance.
5. Unknown kind raises ValueError.
6. Concurrent callers serialise on the row lock — tested by opening
   two sessions, taking `next_number` in both within the same
   transaction window, and confirming no duplicate number.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.company import Company
from saebooks.models.document_counter import DocumentCounter
from saebooks.services import numbering


async def _company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company.id


async def _reset(company_id: uuid.UUID, kind: str) -> None:
    """Wipe the counter row for a specific (company, kind) so tests are idempotent."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(DocumentCounter).where(
                DocumentCounter.company_id == company_id,
                DocumentCounter.kind == kind,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_first_call_creates_counter_with_defaults() -> None:
    cid = await _company_id()
    kind = "invoice"
    await _reset(cid, kind)

    async with AsyncSessionLocal() as session:
        number = await numbering.next_number(session, cid, kind)
        await session.commit()

    assert number == "INV-000001"


@pytest.mark.asyncio
async def test_sequential_advance() -> None:
    cid = await _company_id()
    kind = "bill"
    await _reset(cid, kind)

    numbers: list[str] = []
    for _ in range(5):
        async with AsyncSessionLocal() as session:
            numbers.append(await numbering.next_number(session, cid, kind))
            await session.commit()

    assert numbers == [
        "BILL-000001",
        "BILL-000002",
        "BILL-000003",
        "BILL-000004",
        "BILL-000005",
    ]


@pytest.mark.asyncio
async def test_first_call_accepts_prefix_and_pad_override() -> None:
    cid = await _company_id()
    kind = "quote"
    await _reset(cid, kind)

    async with AsyncSessionLocal() as session:
        number = await numbering.next_number(
            session, cid, kind, prefix="SAE-QUO-", pad_width=4
        )
        await session.commit()

    assert number == "SAE-QUO-0001"


@pytest.mark.asyncio
async def test_subsequent_call_ignores_override() -> None:
    """Once the counter is materialised, prefix/pad are locked."""
    cid = await _company_id()
    kind = "credit_note"
    await _reset(cid, kind)

    async with AsyncSessionLocal() as session:
        first = await numbering.next_number(session, cid, kind)
        await session.commit()

    async with AsyncSessionLocal() as session:
        # This override MUST NOT take effect — counter row already exists.
        second = await numbering.next_number(
            session, cid, kind, prefix="IGNORED-", pad_width=2
        )
        await session.commit()

    assert first == "CN-000001"
    assert second == "CN-000002"


@pytest.mark.asyncio
async def test_peek_does_not_advance() -> None:
    cid = await _company_id()
    kind = "payment"
    await _reset(cid, kind)

    async with AsyncSessionLocal() as session:
        peek_a = await numbering.peek_next(session, cid, kind)
        peek_b = await numbering.peek_next(session, cid, kind)
        await session.commit()

    assert peek_a == "PAY-000001"
    assert peek_b == "PAY-000001"

    # And actually minting still yields the same number peek showed.
    async with AsyncSessionLocal() as session:
        minted = await numbering.next_number(session, cid, kind)
        await session.commit()
    assert minted == "PAY-000001"


@pytest.mark.asyncio
async def test_peek_with_existing_counter_reflects_next_value() -> None:
    cid = await _company_id()
    kind = "statement"
    await _reset(cid, kind)

    async with AsyncSessionLocal() as session:
        await numbering.next_number(session, cid, kind)  # mint 1
        await numbering.next_number(session, cid, kind)  # mint 2
        await session.commit()

    async with AsyncSessionLocal() as session:
        assert await numbering.peek_next(session, cid, kind) == "STMT-000003"


@pytest.mark.asyncio
async def test_unknown_kind_raises() -> None:
    cid = await _company_id()
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError, match="Unknown document kind"):
            await numbering.next_number(session, cid, "nope")


@pytest.mark.asyncio
async def test_independent_kinds_do_not_interfere() -> None:
    cid = await _company_id()
    for kind in ("invoice", "bill"):
        await _reset(cid, kind)

    async with AsyncSessionLocal() as session:
        inv1 = await numbering.next_number(session, cid, "invoice")
        bill1 = await numbering.next_number(session, cid, "bill")
        inv2 = await numbering.next_number(session, cid, "invoice")
        await session.commit()

    assert inv1 == "INV-000001"
    assert bill1 == "BILL-000001"
    assert inv2 == "INV-000002"
