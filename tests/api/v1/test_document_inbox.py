"""Contract tests for ``/api/v1/inbox`` (Document Inbox phase 1).

The vault is mocked at the ``saebooks.services.vault`` module boundary
and extraction at ``saebooks.services.ai_extraction.extract_document``
(the service calls it through the module reference), the same way the
existing attachment tests mock the vault. No HTTP leaves the process.

Coverage:

* Gates — 401 without bearer, 404 below Offline edition, 503 vault off.
* Upload — happy path (Business: extraction runs), Offline (extraction
  skipped, NEEDS_REVIEW empty), duplicate double-tap → 200 + duplicate,
  MIME/size/HEIC/empty validation, extraction soft-fail vs transport-fail.
* Dedupe race — pre-check miss + partial-unique violation returns the
  winner's row and soft-archives the fresh blob (service level).
* List — default excludes terminal, status filter, pagination shape.
* Detail + download + stats.
* PATCH — override write, version optimistic lock 409, extract immutable
  (422), READY promotion on completeness.
* Extract retry — dual flag gate, terminal 409, counters reset.
* Publish — EXPENSE only (422 otherwise), X-Idempotency-Key required
  (428), replay returns the same expense (one expense total), draft
  expense created via the standard service path with the blob linked,
  change_log provenance, illegal state 409.
* Reject — vault soft-delete + terminal state, re-reject 409.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.config import settings as _settings
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog
from saebooks.models.contact import Contact
from saebooks.models.expense import Expense
from saebooks.models.inbox_document import InboxDocument
from saebooks.models.tax_code import TaxCode
from saebooks.services import ai_extraction
from saebooks.services import document_inbox as inbox_svc
from saebooks.services import vault as vault_client

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings, "vault_enabled", True)
    monkeypatch.setattr(_settings, "vault_shared_secret", "test-secret")


@pytest.fixture(autouse=True)
def _default_vault_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every test gets a working in-memory vault unless it overrides a
    call. ``calls`` records what the engine asked the vault to do."""
    calls: dict[str, list[Any]] = {
        "upload": [], "download": [], "delete": [], "link": [],
    }
    blobs: dict[uuid.UUID, bytes] = {}

    async def fake_upload(tenant_id, *, file, filename, content_type, actor=None):
        fid = uuid.uuid4()
        blobs[fid] = bytes(file)
        calls["upload"].append({"tenant_id": tenant_id, "filename": filename})
        return {
            "id": str(fid),
            "tenant_id": str(tenant_id),
            "filename": filename,
            "mime": content_type,
            "size_bytes": len(file),
            "sha256": "unused",
        }

    async def fake_download(tenant_id, file_id):
        calls["download"].append(file_id)
        return blobs.get(file_id, b"BYTES"), "image/jpeg", "receipt.jpg"

    async def fake_delete(tenant_id, file_id):
        calls["delete"].append(file_id)

    async def fake_link(tenant_id, file_id, *, entity_kind, entity_id, actor=None):
        calls["link"].append(
            {"file_id": file_id, "entity_kind": entity_kind, "entity_id": entity_id}
        )
        return {"id": str(uuid.uuid4())}

    async def fake_stream(tenant_id, file_id):
        yield blobs.get(file_id, b"BYTES"), "image/jpeg", "receipt.jpg"

    monkeypatch.setattr(vault_client, "upload", fake_upload)
    monkeypatch.setattr(vault_client, "download", fake_download)
    monkeypatch.setattr(vault_client, "delete", fake_delete)
    monkeypatch.setattr(vault_client, "link", fake_link)
    monkeypatch.setattr(vault_client, "stream_download", fake_stream)
    return calls


@pytest.fixture(autouse=True)
def _default_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic model output — overridden per test where needed."""

    async def fake_extract(file_bytes, mime_type, *, settings=None):
        return _extract_result(vendor_name="BP Wacol", total="110.00")

    monkeypatch.setattr(ai_extraction, "extract_document", fake_extract)


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


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {current_token()}"}


@pytest.fixture
async def business_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """Inbox on + AI extraction on (Business tier)."""
    monkeypatch.setattr(_settings, "edition", "business")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=_bearer()
    ) as ac:
        yield ac


@pytest.fixture
async def offline_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """Inbox on, AI extraction OFF (Offline tier)."""
    monkeypatch.setattr(_settings, "edition", "offline")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=_bearer()
    ) as ac:
        yield ac


@pytest.fixture
async def community_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setattr(_settings, "edition", "community")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=_bearer()
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def coding_deps() -> dict[str, str]:
    """Seeded company/contact/accounts/tax-code UUIDs for publish + READY."""
    async with AsyncSessionLocal() as session:
        contact = (
            await session.execute(
                select(Contact)
                .where(
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                    Contact.archived_at.is_(None),
                )
                .limit(1)
            )
        ).scalars().first()
        assert contact is not None
        asset = (
            await session.execute(
                select(Account).where(
                    Account.tenant_id == DEFAULT_TENANT_ID,
                    Account.archived_at.is_(None),
                    Account.is_header.is_(False),
                    Account.account_type == AccountType.ASSET,
                ).limit(1)
            )
        ).scalars().first()
        expense_acct = (
            await session.execute(
                select(Account).where(
                    Account.tenant_id == DEFAULT_TENANT_ID,
                    Account.archived_at.is_(None),
                    Account.is_header.is_(False),
                    Account.account_type == AccountType.EXPENSE,
                ).limit(1)
            )
        ).scalars().first()
        tax_code = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.tenant_id == DEFAULT_TENANT_ID,
                    TaxCode.company_id == contact.company_id,
                ).limit(1)
            )
        ).scalars().first()
        assert asset is not None and expense_acct is not None
        return {
            "company_id": str(contact.company_id),
            "contact_id": str(contact.id),
            "payment_account_id": str(asset.id),
            "expense_account_id": str(expense_acct.id),
            "tax_code_id": str(tax_code.id) if tax_code else "",
        }


def _unique_file(mime: str = "image/jpeg") -> tuple[str, bytes, str]:
    """Unique bytes per call — the DB persists across tests and the
    (tenant, sha256) partial unique would otherwise cross-fire."""
    return ("receipt.jpg", b"JPEG" + uuid.uuid4().bytes + os.urandom(8), mime)


async def _upload(client: AsyncClient, **form: Any) -> Any:
    files = {"file": form.pop("file", _unique_file())}
    return await client.post("/api/v1/inbox/documents", data=form, files=files)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


async def test_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/inbox/documents")
    assert r.status_code == 401


async def test_community_edition_404s(community_client: AsyncClient) -> None:
    r = await community_client.get("/api/v1/inbox/documents")
    assert r.status_code == 404


async def test_vault_disabled_503(
    business_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_settings, "vault_enabled", False)
    r = await business_client.get("/api/v1/inbox/documents")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def test_upload_happy_path_business(
    business_client: AsyncClient, _default_vault_stubs: dict
) -> None:
    r = await _upload(business_client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "NEEDS_REVIEW"  # extracted, not human-coded yet
    assert body["duplicate"] is False
    assert body["source"] == "UPLOAD"
    assert body["extract"]["vendor_name"] == "BP Wacol"
    assert body["extract"]["total"] == "110.00"
    assert body["extraction_confidence"] == "OK"
    assert body["extraction_error"] is None
    assert body["extract_model"]  # provenance recorded
    assert body["attempt_count"] == 1
    assert body["vault_file_id"]
    assert len(_default_vault_stubs["upload"]) == 1
    assert len(_default_vault_stubs["download"]) == 1  # extraction re-read the blob


async def test_upload_offline_skips_extraction(
    offline_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def boom(*a: Any, **kw: Any) -> dict:
        nonlocal called
        called = True
        return _extract_result()

    monkeypatch.setattr(ai_extraction, "extract_document", boom)
    r = await _upload(offline_client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "NEEDS_REVIEW"  # manual keying
    assert body["extract"] is None
    assert body["extraction_confidence"] is None
    assert called is False  # the model was never consulted


async def test_upload_duplicate_double_tap(business_client: AsyncClient) -> None:
    file = _unique_file()
    r1 = await _upload(business_client, file=file)
    assert r1.status_code == 201
    r2 = await _upload(business_client, file=file)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["duplicate"] is True
    assert body["id"] == r1.json()["id"]  # existing row, nothing new stored


async def test_upload_rejected_hash_is_reuploadable(
    business_client: AsyncClient,
) -> None:
    file = _unique_file()
    r1 = await _upload(business_client, file=file)
    doc = r1.json()
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/reject",
        json={"reason": "NOT_A_DOCUMENT"},
    )
    assert r.status_code == 200, r.text
    r2 = await _upload(business_client, file=file)
    assert r2.status_code == 201  # fresh row — a mistaken reject is recoverable
    assert r2.json()["id"] != doc["id"]


@pytest.mark.parametrize(
    ("mime", "needle"),
    [
        ("image/heic", "convert to JPEG"),
        ("text/plain", "Unsupported file type"),
    ],
)
async def test_upload_bad_mime_422(
    business_client: AsyncClient, mime: str, needle: str
) -> None:
    r = await _upload(business_client, file=("f.bin", b"x" * 10, mime))
    assert r.status_code == 422
    assert needle in r.text


async def test_upload_empty_400_and_oversize_422(
    business_client: AsyncClient,
) -> None:
    r = await _upload(business_client, file=("f.jpg", b"", "image/jpeg"))
    assert r.status_code == 400
    r = await _upload(
        business_client,
        file=("f.jpg", b"x" * (10 * 1024 * 1024 + 1), "image/jpeg"),
    )
    assert r.status_code == 422
    assert "too large" in r.text


async def test_upload_foreign_company_404(business_client: AsyncClient) -> None:
    r = await _upload(business_client, company_id=str(uuid.uuid4()))
    assert r.status_code == 404


async def test_upload_extraction_soft_fail_needs_review_partial(
    business_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def partial(*a: Any, **kw: Any) -> dict:
        return _extract_result(extraction_error="JSON parse error: boom")

    monkeypatch.setattr(ai_extraction, "extract_document", partial)
    r = await _upload(business_client)
    assert r.status_code == 201, r.text  # capture never blocks on the brain
    body = r.json()
    assert body["status"] == "NEEDS_REVIEW"
    assert body["extraction_confidence"] == "PARTIAL"
    assert "JSON parse error" in body["extraction_error"]


async def test_upload_extraction_transport_fail_back_to_received(
    business_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*a: Any, **kw: Any) -> dict:
        raise ai_extraction.AiExtractionNotConfiguredError("no LITELLM key")

    monkeypatch.setattr(ai_extraction, "extract_document", down)
    r = await _upload(business_client)
    assert r.status_code == 201, r.text  # upload still succeeds
    body = r.json()
    assert body["status"] == "RECEIVED"
    assert "no LITELLM key" in body["last_error"]
    assert body["extract"] is None


async def test_upload_vault_down_502(
    business_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*a: Any, **kw: Any) -> dict:
        raise vault_client.VaultUnavailable("simulated outage")

    monkeypatch.setattr(vault_client, "upload", boom)
    r = await _upload(business_client)
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# Dedupe race (service level — forces the partial-unique backstop)
# ---------------------------------------------------------------------------


async def test_ingest_race_backstop_returns_winner_and_archives_fresh_blob(
    business_client: AsyncClient,
    _default_vault_stubs: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate two concurrent uploads: blind the pre-check so the
    second ingest INSERTs into the (tenant, sha256) partial unique. The
    loser must hand back the winner's row and soft-archive its blob."""
    data = b"RACE" + uuid.uuid4().bytes

    async with AsyncSessionLocal() as session:
        doc1, dup1 = await inbox_svc.ingest(
            session,
            DEFAULT_TENANT_ID,
            data=data,
            filename="race.jpg",
            mime="image/jpeg",
            source="UPLOAD",
            extract_enabled=False,
        )
        assert dup1 is False

    real_precheck = inbox_svc._find_active_duplicate
    calls = {"n": 0}

    async def blind_first_call(session, tenant_id, sha256):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the racing request hasn't seen the winner yet
        return await real_precheck(session, tenant_id, sha256)

    monkeypatch.setattr(inbox_svc, "_find_active_duplicate", blind_first_call)

    deletes_before = len(_default_vault_stubs["delete"])
    async with AsyncSessionLocal() as session:
        doc2, dup2 = await inbox_svc.ingest(
            session,
            DEFAULT_TENANT_ID,
            data=data,
            filename="race.jpg",
            mime="image/jpeg",
            source="UPLOAD",
            extract_enabled=False,
        )
    assert dup2 is True
    assert doc2.id == doc1.id  # the winner's row
    # The loser's freshly-uploaded blob was soft-archived, not leaked.
    assert len(_default_vault_stubs["delete"]) == deletes_before + 1
    assert _default_vault_stubs["delete"][-1] != doc1.vault_file_id


# ---------------------------------------------------------------------------
# List / detail / download / stats
# ---------------------------------------------------------------------------


async def test_list_excludes_terminal_by_default(
    business_client: AsyncClient,
) -> None:
    r1 = await _upload(business_client)
    live_id = r1.json()["id"]
    r2 = await _upload(business_client)
    rejected_id = r2.json()["id"]
    r = await business_client.post(
        f"/api/v1/inbox/documents/{rejected_id}/reject",
        json={"reason": "PERSONAL", "note": "coffee"},
    )
    assert r.status_code == 200

    r = await business_client.get("/api/v1/inbox/documents", params={"page_size": 200})
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()["items"]]
    assert live_id in ids
    assert rejected_id not in ids

    # Explicit status filter surfaces the terminal row.
    r = await business_client.get(
        "/api/v1/inbox/documents", params={"status": "REJECTED", "page_size": 200}
    )
    assert rejected_id in [d["id"] for d in r.json()["items"]]


async def test_list_pagination_shape_and_bad_filters(
    business_client: AsyncClient,
) -> None:
    await _upload(business_client)
    r = await business_client.get(
        "/api/v1/inbox/documents", params={"page": 1, "page_size": 1}
    )
    body = r.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert len(body["items"]) == 1
    assert body["total"] >= 1

    assert (
        await business_client.get(
            "/api/v1/inbox/documents", params={"status": "NOPE"}
        )
    ).status_code == 400
    assert (
        await business_client.get(
            "/api/v1/inbox/documents", params={"source": "CARRIER_PIGEON"}
        )
    ).status_code == 400


async def test_detail_and_foreign_id_404(business_client: AsyncClient) -> None:
    doc = (await _upload(business_client)).json()
    r = await business_client.get(f"/api/v1/inbox/documents/{doc['id']}")
    assert r.status_code == 200
    assert r.json()["extract"]["vendor_name"] == "BP Wacol"

    r = await business_client.get(f"/api/v1/inbox/documents/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_download_streams_blob(business_client: AsyncClient) -> None:
    file = _unique_file()
    doc = (await _upload(business_client, file=file)).json()
    r = await business_client.get(f"/api/v1/inbox/documents/{doc['id']}/download")
    assert r.status_code == 200
    assert r.content == file[1]  # the exact bytes we uploaded
    assert "attachment" in r.headers["content-disposition"]


async def test_stats_shape(business_client: AsyncClient) -> None:
    await _upload(business_client)
    r = await business_client.get("/api/v1/inbox/stats")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "RECEIVED", "NEEDS_REVIEW", "READY", "FAILED",
        "oldest_unextracted_age_s", "advisory_duplicates",
    }
    assert body["NEEDS_REVIEW"] >= 1


# ---------------------------------------------------------------------------
# PATCH — review edits
# ---------------------------------------------------------------------------


def _complete_override(deps: dict[str, str]) -> dict[str, Any]:
    return {
        "contact_id": deps["contact_id"],
        "total": "110.00",
        "line_items": [
            {
                "description": "Fuel",
                "account_id": deps["expense_account_id"],
                "tax_code_id": deps["tax_code_id"],
                "unit_price": "100.00",
            }
        ],
    }


async def test_patch_override_promotes_to_ready(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    doc = (await _upload(business_client)).json()
    override = _complete_override(coding_deps)
    r = await business_client.patch(
        f"/api/v1/inbox/documents/{doc['id']}",
        json={"version": doc["version"], "extraction_override": override},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "READY"
    assert body["version"] == doc["version"] + 1
    assert body["extraction_override"] == override
    assert body["extract"] == doc["extract"]  # untouched


async def test_patch_version_mismatch_409(business_client: AsyncClient) -> None:
    doc = (await _upload(business_client)).json()
    r = await business_client.patch(
        f"/api/v1/inbox/documents/{doc['id']}",
        json={"version": doc["version"] + 41, "extraction_override": {}},
    )
    assert r.status_code == 409


async def test_patch_cannot_touch_extract_or_status(
    business_client: AsyncClient,
) -> None:
    doc = (await _upload(business_client)).json()
    for forbidden in ({"extract": {"total": "1.00"}}, {"status": "READY"}):
        r = await business_client.patch(
            f"/api/v1/inbox/documents/{doc['id']}",
            json={"version": doc["version"], **forbidden},
        )
        assert r.status_code == 422, r.text


async def test_patch_company_routing(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    doc = (await _upload(business_client)).json()
    r = await business_client.patch(
        f"/api/v1/inbox/documents/{doc['id']}",
        json={"version": doc["version"], "company_id": coding_deps["company_id"]},
    )
    assert r.status_code == 200
    assert r.json()["company_id"] == coding_deps["company_id"]

    r = await business_client.patch(
        f"/api/v1/inbox/documents/{doc['id']}",
        json={"version": doc["version"] + 1, "company_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404  # foreign/unknown company


# ---------------------------------------------------------------------------
# Extract retry
# ---------------------------------------------------------------------------


async def test_retry_reruns_extraction(
    business_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*a: Any, **kw: Any) -> dict:
        raise ai_extraction.AiExtractionNotConfiguredError("no key")

    monkeypatch.setattr(ai_extraction, "extract_document", down)
    doc = (await _upload(business_client)).json()
    assert doc["status"] == "RECEIVED"

    async def up(*a: Any, **kw: Any) -> dict:
        return _extract_result(vendor_name="Bunnings", total="42.00")

    monkeypatch.setattr(ai_extraction, "extract_document", up)
    r = await business_client.post(f"/api/v1/inbox/documents/{doc['id']}/extract")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "NEEDS_REVIEW"
    assert body["extract"]["vendor_name"] == "Bunnings"
    assert body["attempt_count"] == 1  # counters were reset before the run
    assert body["last_error"] is None


async def test_retry_gated_by_ai_flag(offline_client: AsyncClient) -> None:
    doc = (await _upload(offline_client)).json()
    r = await offline_client.post(f"/api/v1/inbox/documents/{doc['id']}/extract")
    assert r.status_code == 404  # FLAG_AI_EXTRACTION is Business+


async def test_retry_on_terminal_doc_409(business_client: AsyncClient) -> None:
    doc = (await _upload(business_client)).json()
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/reject", json={"reason": "OTHER"}
    )
    assert r.status_code == 200
    r = await business_client.post(f"/api/v1/inbox/documents/{doc['id']}/extract")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def _publish_body(deps: dict[str, str], **overrides: Any) -> dict[str, Any]:
    body = {
        "record_kind": "EXPENSE",
        "company_id": deps["company_id"],
        "contact_id": deps["contact_id"],
        "date": "2026-06-01",
        "payment_account_id": deps["payment_account_id"],
        "reference": "RCPT-42",
        "lines": [
            {
                "description": "Fuel",
                "account_id": deps["expense_account_id"],
                "quantity": "1",
                "unit_price": "100.00",
            }
        ],
        "notes": "from inbox",
    }
    body.update(overrides)
    return body


async def test_publish_creates_draft_expense_with_provenance(
    business_client: AsyncClient,
    coding_deps: dict[str, str],
    _default_vault_stubs: dict,
) -> None:
    doc = (await _upload(business_client)).json()
    key = f"pub-{uuid.uuid4()}"
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=_publish_body(coding_deps),
        headers={"X-Idempotency-Key": key},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["record"]["kind"] == "EXPENSE"
    assert body["record"]["status"] == "DRAFT"  # never auto-posted
    assert body["document"]["status"] == "PUBLISHED"
    assert body["document"]["published_record_id"] == body["record"]["id"]
    assert body["document"]["published_record_kind"] == "EXPENSE"
    assert body["document"]["published_at"]

    expense_id = uuid.UUID(body["record"]["id"])
    async with AsyncSessionLocal() as session:
        expense = (
            await session.execute(select(Expense).where(Expense.id == expense_id))
        ).scalar_one()
        assert str(expense.status) == "DRAFT"
        # No tax_code on the line → no GST; totals came from the real
        # service path (_recalc), not from anything the inbox computed.
        assert str(expense.total) == "100.00"
        # change_log provenance payload
        log = (
            await session.execute(
                select(ChangeLog).where(
                    ChangeLog.entity == "inbox_document",
                    ChangeLog.entity_id == uuid.UUID(doc["id"]),
                    ChangeLog.op == "publish",
                )
            )
        ).scalars().one()
        assert log.payload["published_record_id"] == str(expense_id)
        assert log.payload["idempotency_key"] == key
        assert log.payload["vault_file_id"] == doc["vault_file_id"]
        assert log.payload["source"] == "UPLOAD"

    # The source blob was linked to the draft expense.
    link = _default_vault_stubs["link"][-1]
    assert link["entity_kind"] == "expense"
    assert link["entity_id"] == expense_id
    assert str(link["file_id"]) == doc["vault_file_id"]


async def test_publish_idempotency_same_key_one_expense(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    doc = (await _upload(business_client)).json()
    key = f"pub-{uuid.uuid4()}"
    payload = _publish_body(coding_deps)

    r1 = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201, r1.text
    r2 = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json() == r1.json()  # verbatim replay

    expense_id = uuid.UUID(r1.json()["record"]["id"])
    async with AsyncSessionLocal() as session:
        count = len(
            (
                await session.execute(
                    select(Expense.id).where(
                        Expense.tenant_id == DEFAULT_TENANT_ID,
                        Expense.reference == "RCPT-42",
                        Expense.id == expense_id,
                    )
                )
            ).scalars().all()
        )
        assert count == 1
        # And no second expense from the replayed call: the doc points at
        # exactly one published record.
        row = (
            await session.execute(
                select(InboxDocument.published_record_id).where(
                    InboxDocument.id == uuid.UUID(doc["id"])
                )
            )
        ).scalar_one()
        assert row == expense_id


async def test_publish_key_reused_with_different_body_422(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    doc = (await _upload(business_client)).json()
    key = f"pub-{uuid.uuid4()}"
    r1 = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=_publish_body(coding_deps),
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201
    r2 = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=_publish_body(coding_deps, reference="DIFFERENT"),
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 422
    assert r2.json()["code"] == "idempotency_key_conflict"


async def test_publish_requires_idempotency_key(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    doc = (await _upload(business_client)).json()
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=_publish_body(coding_deps),
    )
    assert r.status_code == 428


async def test_publish_unknown_kind_422(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    """Phase 2 unlocked BILL/CREDIT_NOTE; anything else is still 422.
    (BILL and CREDIT_NOTE publish paths are covered in
    tests/api/v1/test_document_inbox_rules.py.)"""
    doc = (await _upload(business_client)).json()
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=_publish_body(coding_deps, record_kind="INVOICE"),
        headers={"X-Idempotency-Key": f"pub-{uuid.uuid4()}"},
    )
    assert r.status_code == 422
    assert "EXPENSE, BILL, CREDIT_NOTE" in r.text


async def test_publish_expense_requires_payment_account(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    """payment_account_id became optional in the body (BILL/CREDIT_NOTE
    don't take one) but stays mandatory for EXPENSE."""
    doc = (await _upload(business_client)).json()
    body = _publish_body(coding_deps)
    body.pop("payment_account_id")
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=body,
        headers={"X-Idempotency-Key": f"pub-{uuid.uuid4()}"},
    )
    assert r.status_code == 422
    assert "payment_account_id is required" in r.text


async def test_publish_twice_different_keys_409(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    doc = (await _upload(business_client)).json()
    r1 = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=_publish_body(coding_deps),
        headers={"X-Idempotency-Key": f"pub-{uuid.uuid4()}"},
    )
    assert r1.status_code == 201
    r2 = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=_publish_body(coding_deps),
        headers={"X-Idempotency-Key": f"pub-{uuid.uuid4()}"},
    )
    assert r2.status_code == 409  # PUBLISHED is terminal


async def test_publish_foreign_company_404(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    doc = (await _upload(business_client)).json()
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=_publish_body(coding_deps, company_id=str(uuid.uuid4())),
        headers={"X-Idempotency-Key": f"pub-{uuid.uuid4()}"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


async def test_reject_soft_deletes_blob_and_is_terminal(
    business_client: AsyncClient, _default_vault_stubs: dict
) -> None:
    doc = (await _upload(business_client)).json()
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/reject",
        json={"reason": "PERSONAL", "note": "my coffee"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "REJECTED"
    assert body["reject_reason"] == "PERSONAL"
    assert body["reject_note"] == "my coffee"
    assert str(_default_vault_stubs["delete"][-1]) == doc["vault_file_id"]

    # Terminal — a second reject 409s.
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/reject", json={"reason": "OTHER"}
    )
    assert r.status_code == 409


async def test_reject_bad_reason_422(business_client: AsyncClient) -> None:
    doc = (await _upload(business_client)).json()
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/reject", json={"reason": "MEH"}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Publish atomicity + terminal immutability + shared-blob reject
# (adversarial-review fix pass)
# ---------------------------------------------------------------------------


async def test_publish_midflow_failure_rolls_back_and_key_stays_retryable(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    """The claim, the DRAFT record, the PUBLISHED stamp and the stored
    idempotency response commit in ONE transaction: a mid-publish
    failure must leave no orphan DRAFT record, no PUBLISHED stamp, and
    the idempotency key must NOT be poisoned into a permanent
    IN_FLIGHT 503 — the retry with the same key succeeds."""
    doc = (await _upload(business_client)).json()
    key = f"pub-{uuid.uuid4()}"
    reference = f"ATOMIC-{uuid.uuid4().hex[:8]}"
    payload = _publish_body(coding_deps, reference=reference)

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("mid-publish failure (injected)")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(inbox_svc, "apply_publish_rule_effects", boom)
        try:
            r = await business_client.post(
                f"/api/v1/inbox/documents/{doc['id']}/publish",
                json=payload,
                headers={"X-Idempotency-Key": key},
            )
            assert r.status_code >= 500  # if the app maps it to a 500
        except RuntimeError:
            pass  # ASGITransport re-raises app exceptions — same outcome

    async with AsyncSessionLocal() as session:
        orphans = (
            await session.execute(
                select(Expense.id).where(
                    Expense.tenant_id == DEFAULT_TENANT_ID,
                    Expense.reference == reference,
                )
            )
        ).scalars().all()
        assert orphans == []  # no orphan DRAFT expense
        row = (
            await session.execute(
                select(InboxDocument).where(
                    InboxDocument.id == uuid.UUID(doc["id"])
                )
            )
        ).scalar_one()
        assert str(row.status) != "PUBLISHED"
        assert row.published_record_id is None

    # Same key retries cleanly — no IN_FLIGHT poison, exactly one expense.
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r.status_code == 201, r.text
    async with AsyncSessionLocal() as session:
        created = (
            await session.execute(
                select(Expense.id).where(
                    Expense.tenant_id == DEFAULT_TENANT_ID,
                    Expense.reference == reference,
                )
            )
        ).scalars().all()
        assert len(created) == 1


async def test_patch_terminal_document_409_immutable(
    business_client: AsyncClient, coding_deps: dict[str, str]
) -> None:
    """PUBLISHED (and REJECTED / DUPLICATE) rows are immutable
    provenance — PATCH must 409, not silently rewrite the override or
    divorce the row from its company."""
    doc = (await _upload(business_client)).json()
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=_publish_body(coding_deps),
        headers={"X-Idempotency-Key": f"pub-{uuid.uuid4()}"},
    )
    assert r.status_code == 201, r.text
    published = r.json()["document"]

    r = await business_client.patch(
        f"/api/v1/inbox/documents/{doc['id']}",
        json={
            "version": published["version"],
            "extraction_override": {"vendor_name": "Tampered Pty Ltd"},
        },
    )
    assert r.status_code == 409
    assert "immutable" in r.text

    # REJECTED is equally closed.
    doc2 = (await _upload(business_client)).json()
    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc2['id']}/reject", json={"reason": "OTHER"}
    )
    assert r.status_code == 200
    r = await business_client.patch(
        f"/api/v1/inbox/documents/{doc2['id']}",
        json={"version": r.json()["version"], "company_id": None},
    )
    assert r.status_code == 409

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(InboxDocument).where(
                    InboxDocument.id == uuid.UUID(doc["id"])
                )
            )
        ).scalar_one()
        assert (row.extraction_override or {}).get("vendor_name") != "Tampered Pty Ltd"


async def test_reject_keeps_blob_shared_with_email_duplicate_rows(
    business_client: AsyncClient, _default_vault_stubs: dict
) -> None:
    """An emailed byte-duplicate row reuses the original's blob (no
    second copy) — rejecting the original must NOT archive a blob that
    live DUPLICATE audit rows still preview/download."""
    doc = (await _upload(business_client)).json()

    # Simulate what ingest_email_attachment stores for a re-sent copy.
    async with AsyncSessionLocal() as session:
        session.add(
            InboxDocument(
                tenant_id=DEFAULT_TENANT_ID,
                vault_file_id=uuid.UUID(doc["vault_file_id"]),
                sha256=doc["sha256"],
                filename="resent.jpg",
                mime="image/jpeg",
                size_bytes=doc["size_bytes"],
                source="EMAIL",
                source_ref=f"msg-{uuid.uuid4()}#0",
                status="DUPLICATE",
                duplicate_of_id=uuid.UUID(doc["id"]),
            )
        )
        await session.commit()

    r = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/reject", json={"reason": "DUPLICATE"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REJECTED"
    # The shared blob was NOT vault-deleted.
    assert uuid.UUID(doc["vault_file_id"]) not in [
        fid if isinstance(fid, uuid.UUID) else uuid.UUID(str(fid))
        for fid in _default_vault_stubs["delete"]
    ]
