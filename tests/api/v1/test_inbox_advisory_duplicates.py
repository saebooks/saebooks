"""Advisory near-duplicate tests — issue #33 phase 4 (spec §6/§9).

sha256 dedupe catches identical bytes; a re-scan of the same paper
invoice produces different bytes. The advisory check flags
(tenant, contact/vendor, invoice_number) collisions among NON-TERMINAL
inbox documents and surfaces them as:

* ``advisory_duplicates`` on the document detail response (compact
  sibling shapes for the review banner), and
* ``advisory_duplicates`` on ``/inbox/stats`` (count of open documents
  in a collision).

Advisory only — nothing is blocked, no status changes. Vault/extraction
are mocked exactly like tests/api/v1/test_document_inbox.py; the fake
extraction result is a per-test mutable holder so each upload can carry
its own vendor/invoice identity.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from saebooks.api.v1.auth import current_token
from saebooks.config import settings as _settings
from saebooks.main import app
from saebooks.services import ai_extraction
from saebooks.services import vault as vault_client

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_document_inbox.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings, "vault_enabled", True)
    monkeypatch.setattr(_settings, "vault_shared_secret", "test-secret")


@pytest.fixture(autouse=True)
def _vault_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    blobs: dict[uuid.UUID, bytes] = {}

    async def fake_upload(tenant_id, *, file, filename, content_type, actor=None):
        fid = uuid.uuid4()
        blobs[fid] = bytes(file)
        return {
            "id": str(fid),
            "tenant_id": str(tenant_id),
            "filename": filename,
            "mime": content_type,
            "size_bytes": len(file),
            "sha256": "unused",
        }

    async def fake_download(tenant_id, file_id):
        return blobs.get(file_id, b"BYTES"), "image/jpeg", "receipt.jpg"

    async def fake_delete(tenant_id, file_id):
        return None

    monkeypatch.setattr(vault_client, "upload", fake_upload)
    monkeypatch.setattr(vault_client, "download", fake_download)
    monkeypatch.setattr(vault_client, "delete", fake_delete)


@pytest.fixture()
def extract_holder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Mutable extraction result — set ``holder["result"]`` before each
    upload to control that document's extracted identity."""
    holder: dict[str, Any] = {"result": _extract_result()}

    async def fake_extract(file_bytes, mime_type, *, settings=None):
        return dict(holder["result"])

    monkeypatch.setattr(ai_extraction, "extract_document", fake_extract)
    return holder


def _extract_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "vendor_name": None,
        "invoice_number": None,
        "date": None,
        "due_date": None,
        "subtotal": None,
        "tax_amount": None,
        "total": None,
        "currency": None,
        "line_items": [],
        "notes": None,
        "extraction_error": None,
    }
    base.update(overrides)
    return base


@pytest.fixture
async def business_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setattr(_settings, "edition", "business")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {current_token()}"},
    ) as ac:
        yield ac


async def _upload(client: AsyncClient) -> dict[str, Any]:
    """Unique bytes per call — sha256 dedupe must never fire here."""
    files = {
        "file": ("receipt.jpg", b"JPEG" + uuid.uuid4().bytes + os.urandom(8), "image/jpeg")
    }
    r = await client.post("/api/v1/inbox/documents", files=files)
    assert r.status_code == 201, r.text
    return r.json()


def _identity() -> tuple[str, str]:
    """Unique (vendor, invoice) per test — the test DB is shared and
    stats are tenant-wide."""
    tag = uuid.uuid4().hex[:8]
    return f"Vendor {tag} Pty Ltd", f"INV-{tag}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_rescan_same_vendor_and_invoice_flags_both(
    business_client: AsyncClient, extract_holder: dict[str, Any]
) -> None:
    vendor, inv = _identity()
    extract_holder["result"] = _extract_result(
        vendor_name=vendor, invoice_number=inv, total="110.00"
    )
    a = await _upload(business_client)
    # Re-scan: different bytes (sha misses), same identity — vendor name
    # normalisation must also collide case/whitespace variants.
    extract_holder["result"] = _extract_result(
        vendor_name=f"  {vendor.upper()} ", invoice_number=inv.lower(), total="110.00"
    )
    b = await _upload(business_client)

    detail_b = (
        await business_client.get(f"/api/v1/inbox/documents/{b['id']}")
    ).json()
    assert [d["id"] for d in detail_b["advisory_duplicates"]] == [a["id"]]
    entry = detail_b["advisory_duplicates"][0]
    assert entry["invoice_number"] == inv
    assert entry["vendor_name"] == vendor
    assert entry["status"] in ("NEEDS_REVIEW", "READY")
    assert entry["filename"] == "receipt.jpg"

    # Symmetric — the earlier document warns about the later one too.
    detail_a = (
        await business_client.get(f"/api/v1/inbox/documents/{a['id']}")
    ).json()
    assert [d["id"] for d in detail_a["advisory_duplicates"]] == [b["id"]]

    stats = (await business_client.get("/api/v1/inbox/stats")).json()
    assert stats["advisory_duplicates"] >= 2


async def test_same_invoice_number_different_vendor_no_match(
    business_client: AsyncClient, extract_holder: dict[str, Any]
) -> None:
    vendor_a, inv = _identity()
    vendor_b, _ = _identity()
    extract_holder["result"] = _extract_result(vendor_name=vendor_a, invoice_number=inv)
    await _upload(business_client)
    extract_holder["result"] = _extract_result(vendor_name=vendor_b, invoice_number=inv)
    b = await _upload(business_client)

    detail = (await business_client.get(f"/api/v1/inbox/documents/{b['id']}")).json()
    assert detail["advisory_duplicates"] == []


async def test_no_invoice_number_never_flags(
    business_client: AsyncClient, extract_holder: dict[str, Any]
) -> None:
    vendor, _ = _identity()
    extract_holder["result"] = _extract_result(vendor_name=vendor, invoice_number=None)
    await _upload(business_client)
    b = await _upload(business_client)
    detail = (await business_client.get(f"/api/v1/inbox/documents/{b['id']}")).json()
    assert detail["advisory_duplicates"] == []


async def test_override_identity_wins_over_extract(
    business_client: AsyncClient, extract_holder: dict[str, Any]
) -> None:
    """The reviewer-effective view (extract ⊕ override) drives the check:
    correcting the invoice number in the override creates the collision
    the raw extract missed."""
    vendor, inv = _identity()
    extract_holder["result"] = _extract_result(vendor_name=vendor, invoice_number=inv)
    a = await _upload(business_client)
    extract_holder["result"] = _extract_result(
        vendor_name=vendor, invoice_number=f"{inv}-misread"
    )
    b = await _upload(business_client)

    detail = (await business_client.get(f"/api/v1/inbox/documents/{b['id']}")).json()
    assert detail["advisory_duplicates"] == []

    r = await business_client.patch(
        f"/api/v1/inbox/documents/{b['id']}",
        json={
            "version": b["version"],
            "extraction_override": {"invoice_number": inv},
        },
    )
    assert r.status_code == 200, r.text
    detail = (await business_client.get(f"/api/v1/inbox/documents/{b['id']}")).json()
    assert [d["id"] for d in detail["advisory_duplicates"]] == [a["id"]]


async def test_rejected_sibling_stops_flagging(
    business_client: AsyncClient, extract_holder: dict[str, Any]
) -> None:
    """Terminal siblings drop out — the banner reflects the OPEN inbox."""
    vendor, inv = _identity()
    extract_holder["result"] = _extract_result(vendor_name=vendor, invoice_number=inv)
    a = await _upload(business_client)
    b = await _upload(business_client)

    detail = (await business_client.get(f"/api/v1/inbox/documents/{b['id']}")).json()
    assert [d["id"] for d in detail["advisory_duplicates"]] == [a["id"]]

    r = await business_client.post(
        f"/api/v1/inbox/documents/{a['id']}/reject",
        json={"reason": "DUPLICATE"},
    )
    assert r.status_code == 200, r.text
    detail = (await business_client.get(f"/api/v1/inbox/documents/{b['id']}")).json()
    assert detail["advisory_duplicates"] == []


async def test_contact_disagreement_beats_vendor_name(
    business_client: AsyncClient, extract_holder: dict[str, Any]
) -> None:
    """When BOTH documents carry a contact, the contacts decide — two
    suppliers sharing a display name must not collide."""
    from sqlalchemy import select

    from saebooks.api.v1.auth import DEFAULT_TENANT_ID
    from saebooks.db import AsyncSessionLocal
    from saebooks.models.contact import Contact

    async with AsyncSessionLocal() as session:
        contacts = (
            (
                await session.execute(
                    select(Contact)
                    .where(
                        Contact.tenant_id == DEFAULT_TENANT_ID,
                        Contact.archived_at.is_(None),
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
    if len(contacts) < 2:
        pytest.skip("needs two seeded contacts")
    contact_a, contact_b = str(contacts[0].id), str(contacts[1].id)

    vendor, inv = _identity()
    extract_holder["result"] = _extract_result(vendor_name=vendor, invoice_number=inv)
    a = await _upload(business_client)
    b = await _upload(business_client)

    for doc, contact in ((a, contact_a), (b, contact_b)):
        r = await business_client.patch(
            f"/api/v1/inbox/documents/{doc['id']}",
            json={
                "version": doc["version"],
                "extraction_override": {"contact_id": contact},
            },
        )
        assert r.status_code == 200, r.text

    detail = (await business_client.get(f"/api/v1/inbox/documents/{b['id']}")).json()
    assert detail["advisory_duplicates"] == []

    # Same contact on both → collide again (contact agreement matches).
    r = await business_client.get(f"/api/v1/inbox/documents/{b['id']}")
    fresh_b = r.json()
    r = await business_client.patch(
        f"/api/v1/inbox/documents/{b['id']}",
        json={
            "version": fresh_b["version"],
            "extraction_override": {"contact_id": contact_a},
        },
    )
    assert r.status_code == 200, r.text
    detail = (await business_client.get(f"/api/v1/inbox/documents/{b['id']}")).json()
    assert [d["id"] for d in detail["advisory_duplicates"]] == [a["id"]]
