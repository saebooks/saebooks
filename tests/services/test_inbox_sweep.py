"""Cron-sweep machinery tests (spec §5): claim / backoff / reclaim.

Runs against the live migrated Postgres with the vault + model mocked
at the module boundary. Each test gets a fresh tenant so the claim
scans never collide with other tests sharing the DB.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from saebooks.db import AsyncSessionLocal
from saebooks.models.inbox_document import InboxDocument, InboxDocumentStatus
from saebooks.models.tenant import Tenant
from saebooks.services import ai_extraction
from saebooks.services import document_inbox as inbox_svc
from saebooks.services import vault as vault_client

pytestmark = pytest.mark.postgres_only

_S = InboxDocumentStatus


@pytest.fixture(autouse=True)
def _vault_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_download(tenant_id, file_id):
        return b"BYTES", "image/jpeg", "receipt.jpg"

    async def fake_extract(file_bytes, mime_type, *, settings=None):
        return {
            "vendor_name": "BP Wacol",
            "total": "110.00",
            "line_items": [],
            "extraction_error": None,
        }

    monkeypatch.setattr(vault_client, "download", fake_download)
    monkeypatch.setattr(ai_extraction, "extract_document", fake_extract)


@pytest.fixture
async def tenant_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        suffix = uuid.uuid4().hex[:8]
        t = Tenant(
            id=uuid.uuid4(), name=f"SWEEP-{suffix}", slug=f"sweep-{suffix}"
        )
        session.add(t)
        await session.commit()
        return t.id


async def _mk_doc(
    tenant_id: uuid.UUID,
    *,
    status: _S = _S.RECEIVED,
    next_attempt_delta_s: int = -1,
    claimed_delta_s: int | None = None,
    attempt_count: int = 0,
) -> uuid.UUID:
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        doc = InboxDocument(
            tenant_id=tenant_id,
            vault_file_id=uuid.uuid4(),
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            filename="r.jpg",
            mime="image/jpeg",
            size_bytes=10,
            source="EMAIL",
            status=status,
            attempt_count=attempt_count,
            next_attempt_at=now + timedelta(seconds=next_attempt_delta_s),
            claimed_at=(
                now + timedelta(seconds=claimed_delta_s)
                if claimed_delta_s is not None
                else None
            ),
        )
        session.add(doc)
        await session.commit()
        return doc.id


async def _get(doc_id: uuid.UUID) -> InboxDocument:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(InboxDocument).where(InboxDocument.id == doc_id)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Backoff schedule (spec §5: 60s·5^(n−1))
# ---------------------------------------------------------------------------


def test_backoff_schedule_exact() -> None:
    assert [inbox_svc.sweep_backoff_delay_s(n) for n in (1, 2, 3, 4)] == [
        60,
        300,
        1500,
        7500,
    ]


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


async def test_claim_batch_of_ten_due_only_oldest_first(
    tenant_id: uuid.UUID,
) -> None:
    due_ids = [
        await _mk_doc(tenant_id, next_attempt_delta_s=-(i + 1))
        for i in range(12)
    ]
    future_id = await _mk_doc(tenant_id, next_attempt_delta_s=3600)

    async with AsyncSessionLocal() as session:
        claimed = await inbox_svc.sweep_claim(session, tenant_id)
    assert len(claimed) == 10  # spec §5 batch
    assert future_id not in claimed
    # Oldest next_attempt_at first — the 10 most-overdue of the 12.
    assert set(claimed) == set(due_ids[2:])

    for doc_id in claimed:
        doc = await _get(doc_id)
        assert str(doc.status) == "EXTRACTING"
        assert doc.claimed_at is not None

    # Second fire picks up the remaining two, not the future one.
    async with AsyncSessionLocal() as session:
        second = await inbox_svc.sweep_claim(session, tenant_id)
    assert set(second) == set(due_ids[:2])
    assert str((await _get(future_id)).status) == "RECEIVED"


async def test_claim_ignores_other_tenants(tenant_id: uuid.UUID) -> None:
    other = await _mk_doc(tenant_id, next_attempt_delta_s=-10)
    async with AsyncSessionLocal() as session:
        suffix = uuid.uuid4().hex[:8]
        t2 = Tenant(id=uuid.uuid4(), name=f"SWEEP2-{suffix}", slug=f"sweep2-{suffix}")
        session.add(t2)
        await session.commit()
        claimed = await inbox_svc.sweep_claim(session, t2.id)
    assert claimed == []
    assert str((await _get(other)).status) == "RECEIVED"


# ---------------------------------------------------------------------------
# Reclaim (EXTRACTING with a stale claim)
# ---------------------------------------------------------------------------


async def test_reclaim_stale_extracting_only(tenant_id: uuid.UUID) -> None:
    stale = await _mk_doc(
        tenant_id, status=_S.EXTRACTING, claimed_delta_s=-660  # 11 min
    )
    fresh = await _mk_doc(
        tenant_id, status=_S.EXTRACTING, claimed_delta_s=-300  # 5 min
    )
    async with AsyncSessionLocal() as session:
        count = await inbox_svc.sweep_reclaim(session, tenant_id)
    assert count == 1
    reclaimed = await _get(stale)
    assert str(reclaimed.status) == "RECEIVED"
    assert reclaimed.claimed_at is None
    assert str((await _get(fresh)).status) == "EXTRACTING"


async def test_reclaimed_doc_is_claimable_again(tenant_id: uuid.UUID) -> None:
    stale = await _mk_doc(
        tenant_id, status=_S.EXTRACTING, claimed_delta_s=-3600
    )
    async with AsyncSessionLocal() as session:
        await inbox_svc.sweep_reclaim(session, tenant_id)
        claimed = await inbox_svc.sweep_claim(session, tenant_id)
    assert claimed == [stale]


# ---------------------------------------------------------------------------
# Process — success / degradation / lost claim
# ---------------------------------------------------------------------------


async def test_process_success_writes_extract(tenant_id: uuid.UUID) -> None:
    doc_id = await _mk_doc(tenant_id)
    async with AsyncSessionLocal() as session:
        [claimed] = await inbox_svc.sweep_claim(session, tenant_id)
        doc = await inbox_svc.sweep_process_claimed(session, tenant_id, claimed)
    assert doc is not None and doc.id == doc_id
    assert str(doc.status) == "NEEDS_REVIEW"
    assert doc.extract["vendor_name"] == "BP Wacol"
    assert doc.attempt_count == 1
    assert doc.claimed_at is None
    assert doc.last_error is None


async def test_process_without_ai_flag_lands_empty_needs_review(
    tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def boom(*a: Any, **kw: Any) -> dict:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(ai_extraction, "extract_document", boom)
    await _mk_doc(tenant_id)
    async with AsyncSessionLocal() as session:
        [claimed] = await inbox_svc.sweep_claim(session, tenant_id)
        doc = await inbox_svc.sweep_process_claimed(
            session, tenant_id, claimed, extract_enabled=False
        )
    assert doc is not None
    assert str(doc.status) == "NEEDS_REVIEW"
    assert doc.extract is None
    assert called is False  # the model was never consulted


async def test_process_lost_claim_returns_none(tenant_id: uuid.UUID) -> None:
    """The doc was finished (or reclaimed) between claim and process —
    mutual safety means a quiet no-op, never an error."""
    doc_id = await _mk_doc(tenant_id, status=_S.NEEDS_REVIEW)
    async with AsyncSessionLocal() as session:
        result = await inbox_svc.sweep_process_claimed(session, tenant_id, doc_id)
    assert result is None
    assert str((await _get(doc_id)).status) == "NEEDS_REVIEW"


# ---------------------------------------------------------------------------
# Process — transport failure backoff → FAILED after 5
# ---------------------------------------------------------------------------


async def test_transport_failure_schedules_backoff(
    tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*a: Any, **kw: Any) -> dict:
        raise ai_extraction.AiExtractionNotConfiguredError("litellm down")

    monkeypatch.setattr(ai_extraction, "extract_document", down)

    await _mk_doc(tenant_id)
    before = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        [claimed] = await inbox_svc.sweep_claim(session, tenant_id)
        doc = await inbox_svc.sweep_process_claimed(session, tenant_id, claimed)
    assert doc is not None
    assert str(doc.status) == "RECEIVED"
    assert doc.attempt_count == 1
    assert "litellm down" in doc.last_error
    # next_attempt_at ≈ now + 60s (attempt 1).
    delay = (doc.next_attempt_at - before).total_seconds()
    assert 55 <= delay <= 70

    # Not claimable again until the backoff elapses.
    async with AsyncSessionLocal() as session:
        assert await inbox_svc.sweep_claim(session, tenant_id) == []


async def test_second_failure_backs_off_5x_longer(
    tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*a: Any, **kw: Any) -> dict:
        raise ai_extraction.AiExtractionNotConfiguredError("still down")

    monkeypatch.setattr(ai_extraction, "extract_document", down)
    await _mk_doc(tenant_id, attempt_count=1)  # one failure already
    before = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        [claimed] = await inbox_svc.sweep_claim(session, tenant_id)
        doc = await inbox_svc.sweep_process_claimed(session, tenant_id, claimed)
    assert doc is not None
    assert doc.attempt_count == 2
    delay = (doc.next_attempt_at - before).total_seconds()
    assert 295 <= delay <= 310  # 60·5^1


async def test_failed_after_five_attempts(
    tenant_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*a: Any, **kw: Any) -> dict:
        raise ai_extraction.AiExtractionNotConfiguredError("dead")

    monkeypatch.setattr(ai_extraction, "extract_document", down)
    doc_id = await _mk_doc(tenant_id, attempt_count=4)
    async with AsyncSessionLocal() as session:
        [claimed] = await inbox_svc.sweep_claim(session, tenant_id)
        doc = await inbox_svc.sweep_process_claimed(session, tenant_id, claimed)
    assert doc is not None and doc.id == doc_id
    assert str(doc.status) == "FAILED"  # still visible, still hand-keyable
    assert doc.attempt_count == 5

    # FAILED is not claimable — humans (or the retry endpoint) own it now.
    async with AsyncSessionLocal() as session:
        assert await inbox_svc.sweep_claim(session, tenant_id) == []
