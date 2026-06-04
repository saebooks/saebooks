"""Safety tests for Paperless → DRAFT-bill ingest.

The rule: Paperless must never corrupt the books. These pin that the
ingest (a) creates a DRAFT bill, never posted; (b) creates NO GL lines
(total 0); (c) is idempotent on the Paperless doc id; (d) parks an
unmatched supplier on a placeholder contact; (e) is fail-safe when
extraction fails — still a draft shell, never raises.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from saebooks.config import settings
from saebooks.db import AsyncSessionLocal
from saebooks.models.bill import Bill, BillStatus
from saebooks.models.company import Company
from saebooks.services.integrations.paperless import PaperlessAttachment
from saebooks.services.integrations import paperless_ingest

pytestmark = pytest.mark.postgres_only

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _FakeClient:
    """Stand-in for PaperlessClient (async context manager)."""

    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def fetch_attachment(self, document_id: int) -> PaperlessAttachment:
        return PaperlessAttachment(
            document_id=document_id,
            title=f"Invoice {document_id}.pdf",
            browser_url=f"https://paperless.example/documents/{document_id}/details",
            mime_type="application/pdf",
        )

    async def download_content(self, document_id: int):
        return b"%PDF-1.4 fake", "application/pdf"


async def _extract_ok(*_a, **_k):
    return {
        "vendor_name": "Totally New Supplier Pty Ltd",
        "date": "2025-06-15",
        "due_date": "2025-07-15",
        "subtotal": "100.00",
        "tax_amount": "10.00",
        "total": "110.00",
        "line_items": [{"description": "Widgets", "qty": "2", "unit_price": "50.00", "amount": "100.00"}],
        "extraction_error": None,
    }


async def _extract_fail(*_a, **_k):
    return {"extraction_error": "Claude API error", "vendor_name": None, "total": None}


async def _company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        co = (
            await s.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at).limit(1)
            )
        ).scalars().first()
        assert co is not None
        return co.id


@pytest.mark.asyncio
async def test_ingest_creates_draft_no_lines_and_is_idempotent() -> None:
    doc_id = 778001
    with patch.object(paperless_ingest, "PaperlessClient", _FakeClient), \
         patch.object(paperless_ingest, "extract_document", _extract_ok):
        async with AsyncSessionLocal() as s:
            s.info["tenant_id"] = _TENANT
            r1 = await paperless_ingest.ingest_document(
                s, tenant_id=_TENANT, document_id=doc_id, settings=settings
            )
        assert r1["status"] == "created", r1
        bill_id = uuid.UUID(r1["bill_id"])

        # Re-fire the SAME document → no duplicate.
        async with AsyncSessionLocal() as s:
            s.info["tenant_id"] = _TENANT
            r2 = await paperless_ingest.ingest_document(
                s, tenant_id=_TENANT, document_id=doc_id, settings=settings
            )
        assert r2["status"] == "duplicate", r2
        assert r2["bill_id"] == r1["bill_id"]

    # The bill is a DRAFT, never posted, with NO GL lines (total 0).
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        bill = (
            await s.execute(select(Bill).where(Bill.id == bill_id))
        ).scalars().first()
        assert bill is not None
        assert bill.status == BillStatus.DRAFT
        assert bill.posted_at is None
        assert bill.total == 0
        assert bill.supplier_reference == f"PL-{doc_id}"
        assert "AUTO-INGESTED FROM PAPERLESS" in (bill.notes or "")
        assert "Totally New Supplier Pty Ltd" in (bill.notes or "")
        # Unmatched supplier parked on the placeholder.
        assert r1["placeholder_supplier"] is True
        # Exactly one bill for this doc (idempotency held).
        count = len(
            (await s.execute(select(Bill).where(Bill.supplier_reference == f"PL-{doc_id}"))).scalars().all()
        )
        assert count == 1


@pytest.mark.asyncio
async def test_ingest_failsafe_on_extraction_error() -> None:
    doc_id = 778002

    class _BoomClient(_FakeClient):
        async def download_content(self, document_id: int):
            raise RuntimeError("paperless unreachable")

    with patch.object(paperless_ingest, "PaperlessClient", _BoomClient), \
         patch.object(paperless_ingest, "extract_document", _extract_fail):
        async with AsyncSessionLocal() as s:
            s.info["tenant_id"] = _TENANT
            r = await paperless_ingest.ingest_document(
                s, tenant_id=_TENANT, document_id=doc_id, settings=settings
            )
    # Still a draft shell — never raised, never posted.
    assert r["status"] == "created", r
    async with AsyncSessionLocal() as s:
        s.info["tenant_id"] = _TENANT
        bill = (
            await s.execute(select(Bill).where(Bill.supplier_reference == f"PL-{doc_id}"))
        ).scalars().first()
        assert bill is not None
        assert bill.status == BillStatus.DRAFT
        assert bill.total == 0
        assert "Extraction incomplete" in (bill.notes or "")


def test_extract_document_id_from_doc_url() -> None:
    """Paperless sends only {{ doc_url }} — pull the pk out of the URL."""
    extract = paperless_ingest.extract_document_id
    assert extract({"doc_url": "https://paperless.x/documents/137/"}) == 137
    assert extract({"doc_url": "http://host:8000/documents/9"}) == 9
    assert extract({"document_url": "/documents/42/details"}) == 42
    # Explicit id keys still win and take precedence over a url.
    assert extract({"document_id": 5, "doc_url": "/documents/137/"}) == 5
    # Nested document dict still works.
    assert extract({"document": {"pk": 88}}) == 88
    # No id anywhere -> None (handler then skips ingest, still 200).
    assert extract({"type": "document_added"}) is None
    assert extract({"doc_url": "https://paperless.x/no-id-here/"}) is None
