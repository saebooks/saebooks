"""Contract tests for Document Inbox phase 2 (issue #33): supplier
rules + BILL/CREDIT_NOTE publish.

Same mocking posture as ``test_document_inbox.py`` — the vault is
stubbed at the ``saebooks.services.vault`` module boundary and
extraction at ``saebooks.services.ai_extraction.extract_document``. No
HTTP leaves the process.

Coverage:

* Supplier-rules endpoints — create (vendor_key/ABN normalisation),
  duplicate-active 409 + soft-delete frees the slot, foreign-FK 422,
  bad ABN / record_kind 422, list default hides inactive, PATCH
  updates + 404, cross-check the counters surface in the shape.
* Matching (extraction time, suggestion-only) — ABN-exact beats
  vendor_key-exact, vendor-name normalisation, READY promotion off a
  fully-coded rule, no-match leaves suggestions null, re-extraction
  clears stale rule suggestions.
* Learn-on-publish — learn_rule creates a LEARNED rule from the
  confirmed values (and it matches the next upload);
  times_applied/times_overridden bookkeeping; update_rule rewrites the
  defaults.
* Publish BILL and CREDIT_NOTE — DRAFT records via the standard
  service paths, blob linked with the right entity kind, provenance
  stamped; no payment_account_id needed.
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
from saebooks.models.account import Account, AccountType
from saebooks.models.bill import Bill
from saebooks.models.contact import Contact, ContactType
from saebooks.models.credit_note import CreditNote
from saebooks.models.supplier_rule import SupplierRule
from saebooks.models.tax_code import TaxCode
from saebooks.services import ai_extraction
from saebooks.services import vault as vault_client

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures (the test_document_inbox.py posture)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings, "vault_enabled", True)
    monkeypatch.setattr(_settings, "vault_shared_secret", "test-secret")


@pytest.fixture(autouse=True)
def _default_vault_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
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
def extract_payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Mutable extraction result — tests set vendor_name/vendor_abn/…
    on this dict BEFORE uploading and the fake model returns it."""
    payload = _extract_result()

    async def fake_extract(file_bytes, mime_type, *, settings=None):
        return dict(payload)

    monkeypatch.setattr(ai_extraction, "extract_document", fake_extract)
    return payload


def _extract_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "vendor_name": None,
        "vendor_abn": None,
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
    monkeypatch.setattr(_settings, "edition", "business")
    async with AsyncClient(
        transport=ASGITransport(app=app_ref()), base_url="http://test",
        headers=_bearer(),
    ) as ac:
        yield ac


def app_ref():
    from saebooks.main import app

    return app


@pytest.fixture
async def deps() -> dict[str, str]:
    """Company + two dedicated supplier contacts + coding accounts."""
    async with AsyncSessionLocal() as session:
        seed_contact = (
            await session.execute(
                select(Contact)
                .where(
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                    Contact.archived_at.is_(None),
                )
                .limit(1)
            )
        ).scalars().first()
        assert seed_contact is not None
        company_id = seed_contact.company_id

        suffix = uuid.uuid4().hex[:8]
        vendors = []
        for i in ("a", "b"):
            c = Contact(
                id=uuid.uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                company_id=company_id,
                name=f"Rules vendor {i} {suffix}",
                contact_type=ContactType.SUPPLIER,
            )
            session.add(c)
            vendors.append(c)
        await session.flush()

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
        income_acct = (
            await session.execute(
                select(Account).where(
                    Account.tenant_id == DEFAULT_TENANT_ID,
                    Account.archived_at.is_(None),
                    Account.is_header.is_(False),
                    Account.account_type == AccountType.INCOME,
                ).limit(1)
            )
        ).scalars().first()
        tax_code = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.tenant_id == DEFAULT_TENANT_ID,
                    TaxCode.company_id == company_id,
                ).limit(1)
            )
        ).scalars().first()
        assert asset is not None and expense_acct is not None
        await session.commit()
        return {
            "company_id": str(company_id),
            "contact_a": str(vendors[0].id),
            "contact_b": str(vendors[1].id),
            "payment_account_id": str(asset.id),
            "expense_account_id": str(expense_acct.id),
            "income_account_id": str(
                (income_acct or expense_acct).id
            ),
            "tax_code_id": str(tax_code.id) if tax_code else "",
        }


def _unique_file(mime: str = "image/jpeg") -> tuple[str, bytes, str]:
    return ("receipt.jpg", b"JPEG" + uuid.uuid4().bytes + os.urandom(8), mime)


async def _upload(client: AsyncClient, **form: Any) -> Any:
    files = {"file": form.pop("file", _unique_file())}
    return await client.post("/api/v1/inbox/documents", data=form, files=files)


def _abn() -> str:
    """Unique syntactically-plausible 11-digit ABN per call."""
    return f"5{uuid.uuid4().int % 10**10:010d}"


def _vendor(name: str = "vendor") -> str:
    return f"{name} {uuid.uuid4().hex[:8]}"


async def _create_rule(
    client: AsyncClient, deps: dict[str, str], **overrides: Any
) -> Any:
    body: dict[str, Any] = {
        "vendor_name": overrides.pop("vendor_name", _vendor()),
        "contact_id": deps["contact_a"],
    }
    body.update(overrides)
    return await client.post("/api/v1/inbox/supplier-rules", json=body)


async def _get_rule_row(rule_id: str) -> SupplierRule:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(SupplierRule).where(SupplierRule.id == uuid.UUID(rule_id))
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Supplier-rules endpoints
# ---------------------------------------------------------------------------


async def test_create_rule_normalises_vendor_and_abn(
    business_client: AsyncClient, deps: dict[str, str]
) -> None:
    suffix = uuid.uuid4().hex[:8]
    abn = _abn()
    spaced_abn = f"{abn[:2]} {abn[2:5]} {abn[5:8]} {abn[8:]}"
    r = await _create_rule(
        business_client,
        deps,
        vendor_name=f"  BP   WACOL {suffix} ",
        vendor_abn=spaced_abn,
        account_id=deps["expense_account_id"],
        tax_code_id=deps["tax_code_id"],
        record_kind="expense",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["vendor_key"] == f"bp wacol {suffix}"  # lower/trim/collapse
    assert body["vendor_abn"] == abn  # digits only
    assert body["record_kind"] == "EXPENSE"  # uppercased
    assert body["origin"] == "MANUAL"
    assert body["active"] is True
    assert body["times_applied"] == 0
    assert body["times_overridden"] == 0

    listed = await business_client.get("/api/v1/inbox/supplier-rules")
    assert body["id"] in [x["id"] for x in listed.json()["items"]]


async def test_create_duplicate_active_409_softdelete_frees_slot(
    business_client: AsyncClient, deps: dict[str, str]
) -> None:
    vendor = _vendor("dup")
    r1 = await _create_rule(business_client, deps, vendor_name=vendor)
    assert r1.status_code == 201
    r2 = await _create_rule(business_client, deps, vendor_name=vendor)
    assert r2.status_code == 409

    # Soft-delete frees the unique slot…
    r = await business_client.patch(
        f"/api/v1/inbox/supplier-rules/{r1.json()['id']}", json={"active": False}
    )
    assert r.status_code == 200
    assert r.json()["active"] is False
    r3 = await _create_rule(business_client, deps, vendor_name=vendor)
    assert r3.status_code == 201

    # …and the inactive rule is hidden from the default list.
    items = (
        await business_client.get("/api/v1/inbox/supplier-rules")
    ).json()["items"]
    ids = [x["id"] for x in items]
    assert r1.json()["id"] not in ids
    assert r3.json()["id"] in ids
    with_inactive = (
        await business_client.get(
            "/api/v1/inbox/supplier-rules", params={"include_inactive": "true"}
        )
    ).json()["items"]
    assert r1.json()["id"] in [x["id"] for x in with_inactive]


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"contact_id": str(uuid.uuid4())}, "contact"),
        ({"vendor_abn": "123"}, "11 digits"),
        ({"record_kind": "INVOICE"}, "record_kind"),
        ({"account_id": str(uuid.uuid4())}, "account"),
    ],
)
async def test_create_rule_bad_input_422(
    business_client: AsyncClient,
    deps: dict[str, str],
    overrides: dict[str, Any],
    needle: str,
) -> None:
    r = await _create_rule(business_client, deps, **overrides)
    assert r.status_code == 422, r.text
    assert needle in r.text


async def test_patch_rule_updates_fields_and_404(
    business_client: AsyncClient, deps: dict[str, str]
) -> None:
    rule = (await _create_rule(business_client, deps)).json()
    r = await business_client.patch(
        f"/api/v1/inbox/supplier-rules/{rule['id']}",
        json={
            "contact_id": deps["contact_b"],
            "account_id": deps["expense_account_id"],
            "vendor_abn": _abn(),
            "record_kind": "BILL",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contact_id"] == deps["contact_b"]
    assert body["account_id"] == deps["expense_account_id"]
    assert body["record_kind"] == "BILL"

    # Explicit null clears a nullable column.
    r = await business_client.patch(
        f"/api/v1/inbox/supplier-rules/{rule['id']}",
        json={"vendor_abn": None, "account_id": None},
    )
    assert r.status_code == 200
    assert r.json()["vendor_abn"] is None
    assert r.json()["account_id"] is None

    r = await business_client.patch(
        f"/api/v1/inbox/supplier-rules/{uuid.uuid4()}", json={"active": False}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Matching — ABN-exact → vendor_key-exact, suggestion-only
# ---------------------------------------------------------------------------


async def test_abn_match_beats_vendor_key_match(
    business_client: AsyncClient,
    deps: dict[str, str],
    extract_payload: dict[str, Any],
) -> None:
    abn = _abn()
    doc_vendor = _vendor("beta")
    # Rule 1 matches by ABN only (different vendor name)…
    r1 = (
        await _create_rule(
            business_client,
            deps,
            vendor_name=_vendor("alpha"),
            vendor_abn=abn,
            contact_id=deps["contact_a"],
        )
    ).json()
    # …rule 2 matches by vendor_key only.
    r2 = (
        await _create_rule(
            business_client,
            deps,
            vendor_name=doc_vendor,
            contact_id=deps["contact_b"],
        )
    ).json()

    extract_payload.update(vendor_name=doc_vendor, vendor_abn=abn)
    doc = (await _upload(business_client)).json()
    assert doc["supplier_rule_id"] == r1["id"], "ABN-exact must win"
    assert doc["suggested_contact_id"] == deps["contact_a"]
    assert doc["supplier_rule_id"] != r2["id"]


async def test_vendor_key_match_is_normalised(
    business_client: AsyncClient,
    deps: dict[str, str],
    extract_payload: dict[str, Any],
) -> None:
    suffix = uuid.uuid4().hex[:8]
    rule = (
        await _create_rule(
            business_client, deps, vendor_name=f"gamma stores {suffix}"
        )
    ).json()
    extract_payload.update(vendor_name=f"  GAMMA   Stores {suffix} ")
    doc = (await _upload(business_client)).json()
    assert doc["supplier_rule_id"] == rule["id"]
    assert doc["suggested_contact_id"] == deps["contact_a"]


async def test_fully_coded_rule_promotes_to_ready(
    business_client: AsyncClient,
    deps: dict[str, str],
    extract_payload: dict[str, Any],
) -> None:
    vendor = _vendor("ready")
    rule = (
        await _create_rule(
            business_client,
            deps,
            vendor_name=vendor,
            account_id=deps["expense_account_id"],
            tax_code_id=deps["tax_code_id"],
        )
    ).json()
    extract_payload.update(
        vendor_name=vendor,
        total="110.00",
        line_items=[
            {"description": "Fuel", "qty": "1", "unit_price": "110.00",
             "amount": "110.00", "tax_code": "GST"}
        ],
    )
    doc = (await _upload(business_client)).json()
    assert doc["supplier_rule_id"] == rule["id"]
    assert doc["suggested_account_id"] == deps["expense_account_id"]
    assert doc["suggested_tax_code_id"] == deps["tax_code_id"]
    assert doc["status"] == "READY", (
        "contact+account+tax from the rule, total+lines from the model "
        "— one-click-publishable (still never auto-published)"
    )


async def test_no_match_leaves_suggestions_null(
    business_client: AsyncClient,
    extract_payload: dict[str, Any],
) -> None:
    extract_payload.update(vendor_name=_vendor("nobody"), vendor_abn=_abn())
    doc = (await _upload(business_client)).json()
    assert doc["supplier_rule_id"] is None
    assert doc["suggested_contact_id"] is None
    assert doc["suggested_account_id"] is None
    assert doc["suggested_tax_code_id"] is None


async def test_reextract_clears_stale_rule_suggestions(
    business_client: AsyncClient,
    deps: dict[str, str],
    extract_payload: dict[str, Any],
) -> None:
    vendor = _vendor("stale")
    rule = (await _create_rule(business_client, deps, vendor_name=vendor)).json()
    extract_payload.update(vendor_name=vendor)
    doc = (await _upload(business_client)).json()
    assert doc["supplier_rule_id"] == rule["id"]

    # Retire the rule, re-extract → suggestions refreshed away.
    r = await business_client.patch(
        f"/api/v1/inbox/supplier-rules/{rule['id']}", json={"active": False}
    )
    assert r.status_code == 200
    r = await business_client.post(f"/api/v1/inbox/documents/{doc['id']}/extract")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["supplier_rule_id"] is None
    assert body["suggested_contact_id"] is None


# ---------------------------------------------------------------------------
# Learn-on-publish + counters
# ---------------------------------------------------------------------------


def _publish_body(
    deps: dict[str, str],
    *,
    record_kind: str = "EXPENSE",
    contact_key: str = "contact_a",
    **overrides: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "record_kind": record_kind,
        "company_id": deps["company_id"],
        "contact_id": deps[contact_key],
        "date": "2026-06-01",
        "reference": f"RCPT-{uuid.uuid4().hex[:6]}",
        "lines": [
            {
                "description": "Fuel",
                "account_id": deps["expense_account_id"],
                "tax_code_id": deps["tax_code_id"],
                "quantity": "1",
                "unit_price": "100.00",
            }
        ],
    }
    if record_kind == "EXPENSE":
        body["payment_account_id"] = deps["payment_account_id"]
    body.update(overrides)
    return body


async def _publish(client: AsyncClient, doc_id: str, body: dict[str, Any]) -> Any:
    return await client.post(
        f"/api/v1/inbox/documents/{doc_id}/publish",
        json=body,
        headers={"X-Idempotency-Key": f"pub-{uuid.uuid4()}"},
    )


async def test_learn_rule_on_publish_creates_learned_rule_that_matches_next(
    business_client: AsyncClient,
    deps: dict[str, str],
    extract_payload: dict[str, Any],
) -> None:
    suffix = uuid.uuid4().hex[:8]
    abn = _abn()
    extract_payload.update(vendor_name=f"Delta   Fuels {suffix}", vendor_abn=abn)
    doc = (await _upload(business_client)).json()
    assert doc["supplier_rule_id"] is None  # nothing to match yet

    r = await _publish(
        business_client, doc["id"], _publish_body(deps, learn_rule=True)
    )
    assert r.status_code == 201, r.text

    # A LEARNED rule now exists, keyed on the normalised vendor,
    # carrying the confirmed values + provenance.
    items = (
        await business_client.get(
            "/api/v1/inbox/supplier-rules", params={"page_size": 200}
        )
    ).json()["items"]
    learned = [x for x in items if x["vendor_key"] == f"delta fuels {suffix}"]
    assert len(learned) == 1
    rule = learned[0]
    assert rule["origin"] == "LEARNED"
    assert rule["vendor_abn"] == abn
    assert rule["contact_id"] == deps["contact_a"]
    assert rule["account_id"] == deps["expense_account_id"]
    assert rule["tax_code_id"] == deps["tax_code_id"]
    assert rule["record_kind"] == "EXPENSE"
    assert rule["created_from_document_id"] == doc["id"]
    assert rule["company_id"] == deps["company_id"]

    # And it matches the next document from the same vendor.
    extract_payload.update(vendor_name=f"DELTA FUELS {suffix}", vendor_abn=None)
    doc2 = (await _upload(business_client)).json()
    assert doc2["supplier_rule_id"] == rule["id"]
    assert doc2["suggested_contact_id"] == deps["contact_a"]


async def test_publish_without_learn_rule_learns_nothing(
    business_client: AsyncClient,
    deps: dict[str, str],
    extract_payload: dict[str, Any],
) -> None:
    suffix = uuid.uuid4().hex[:8]
    extract_payload.update(vendor_name=f"quiet vendor {suffix}")
    doc = (await _upload(business_client)).json()
    r = await _publish(business_client, doc["id"], _publish_body(deps))
    assert r.status_code == 201, r.text
    items = (
        await business_client.get(
            "/api/v1/inbox/supplier-rules", params={"page_size": 200}
        )
    ).json()["items"]
    assert not [x for x in items if x["vendor_key"] == f"quiet vendor {suffix}"]


async def test_counters_applied_then_overridden(
    business_client: AsyncClient,
    deps: dict[str, str],
    extract_payload: dict[str, Any],
) -> None:
    vendor = _vendor("counter")
    rule = (
        await _create_rule(
            business_client, deps, vendor_name=vendor, contact_id=deps["contact_a"]
        )
    ).json()
    extract_payload.update(vendor_name=vendor)

    # Publish 1 — confirms the rule's contact → applied.
    doc1 = (await _upload(business_client)).json()
    assert doc1["supplier_rule_id"] == rule["id"]
    r = await _publish(
        business_client, doc1["id"], _publish_body(deps, contact_key="contact_a")
    )
    assert r.status_code == 201, r.text
    row = await _get_rule_row(rule["id"])
    assert row.times_applied == 1
    assert row.times_overridden == 0
    assert row.last_applied_at is not None

    # Publish 2 — human picks a different contact → overridden; the
    # rule's own defaults stay untouched (no update_rule).
    doc2 = (await _upload(business_client)).json()
    assert doc2["supplier_rule_id"] == rule["id"]
    r = await _publish(
        business_client, doc2["id"], _publish_body(deps, contact_key="contact_b")
    )
    assert r.status_code == 201, r.text
    row = await _get_rule_row(rule["id"])
    assert row.times_applied == 1
    assert row.times_overridden == 1
    assert str(row.contact_id) == deps["contact_a"]  # defaults untouched


async def test_update_rule_true_rewrites_defaults(
    business_client: AsyncClient,
    deps: dict[str, str],
    extract_payload: dict[str, Any],
) -> None:
    vendor = _vendor("rewrite")
    rule = (
        await _create_rule(
            business_client, deps, vendor_name=vendor, contact_id=deps["contact_a"]
        )
    ).json()
    extract_payload.update(vendor_name=vendor)
    doc = (await _upload(business_client)).json()
    assert doc["supplier_rule_id"] == rule["id"]

    r = await _publish(
        business_client,
        doc["id"],
        _publish_body(deps, contact_key="contact_b", update_rule=True),
    )
    assert r.status_code == 201, r.text
    row = await _get_rule_row(rule["id"])
    assert str(row.contact_id) == deps["contact_b"]  # rewritten
    assert str(row.account_id) == deps["expense_account_id"]
    assert str(row.tax_code_id) == deps["tax_code_id"]
    assert row.record_kind == "EXPENSE"
    assert row.times_overridden == 1  # the divergence is still scored


# ---------------------------------------------------------------------------
# Publish — BILL and CREDIT_NOTE (phase 2 unlock)
# ---------------------------------------------------------------------------


async def test_publish_bill_creates_draft_bill(
    business_client: AsyncClient,
    deps: dict[str, str],
    _default_vault_stubs: dict,
) -> None:
    doc = (await _upload(business_client)).json()
    body = _publish_body(deps, record_kind="BILL", due_date="2026-06-30")
    r = await _publish(business_client, doc["id"], body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["record"]["kind"] == "BILL"
    assert out["record"]["status"] == "DRAFT"  # never auto-posted
    assert out["document"]["status"] == "PUBLISHED"
    assert out["document"]["published_record_kind"] == "BILL"
    assert out["document"]["published_record_id"] == out["record"]["id"]

    bill_id = uuid.UUID(out["record"]["id"])
    async with AsyncSessionLocal() as session:
        bill = (
            await session.execute(select(Bill).where(Bill.id == bill_id))
        ).scalar_one()
        assert str(bill.status) == "DRAFT"
        assert bill.supplier_reference == body["reference"]
        assert str(bill.issue_date) == "2026-06-01"
        assert str(bill.due_date) == "2026-06-30"

    link = _default_vault_stubs["link"][-1]
    assert link["entity_kind"] == "bill"
    assert link["entity_id"] == bill_id


async def test_publish_credit_note_creates_draft_credit_note(
    business_client: AsyncClient,
    deps: dict[str, str],
    _default_vault_stubs: dict,
) -> None:
    doc = (await _upload(business_client)).json()
    body = _publish_body(deps, record_kind="CREDIT_NOTE")
    body["lines"][0]["account_id"] = deps["income_account_id"]
    r = await _publish(business_client, doc["id"], body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["record"]["kind"] == "CREDIT_NOTE"
    assert out["record"]["status"] == "DRAFT"
    assert out["document"]["published_record_kind"] == "CREDIT_NOTE"

    cn_id = uuid.UUID(out["record"]["id"])
    async with AsyncSessionLocal() as session:
        cn = (
            await session.execute(
                select(CreditNote).where(CreditNote.id == cn_id)
            )
        ).scalar_one()
        assert str(cn.status) == "DRAFT"
        assert cn.number  # numbering ran through the real service path
        assert str(cn.total) == "110.00"  # 100 + GST via the service _recalc

    link = _default_vault_stubs["link"][-1]
    assert link["entity_kind"] == "credit_note"
    assert link["entity_id"] == cn_id


async def test_publish_credit_note_foreign_contact_422(
    business_client: AsyncClient, deps: dict[str, str]
) -> None:
    """credit_notes.api_create lacks the CIVL-1 tenant validation the
    expense/bill services carry — the router's belt check must catch a
    foreign contact before the record is created."""
    doc = (await _upload(business_client)).json()
    body = _publish_body(deps, record_kind="CREDIT_NOTE")
    body["contact_id"] = str(uuid.uuid4())
    r = await _publish(business_client, doc["id"], body)
    assert r.status_code == 422
    assert "contact" in r.text


async def test_publish_bill_replay_same_key_one_bill(
    business_client: AsyncClient, deps: dict[str, str]
) -> None:
    doc = (await _upload(business_client)).json()
    body = _publish_body(deps, record_kind="BILL")
    key = f"pub-{uuid.uuid4()}"
    r1 = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=body,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201, r1.text
    r2 = await business_client.post(
        f"/api/v1/inbox/documents/{doc['id']}/publish",
        json=body,
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json() == r1.json()  # verbatim replay, one bill total


# ---------------------------------------------------------------------------
# Learn-on-publish unique race (adversarial-review fix pass)
# ---------------------------------------------------------------------------


async def test_learn_rule_unique_collision_adopts_existing_rule(
    business_client: AsyncClient,
    deps: dict[str, str],
    extract_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two near-simultaneous publishes from the same new vendor can both
    miss the rule match and both try to learn — the loser hits
    ``uq_supplier_rules_scope_vendor``. The insert is SAVEPOINT-guarded:
    the loser adopts the winner's rule and the publish succeeds (201),
    instead of a 500 with a poisoned idempotency key.

    Simulated deterministically: a committed rule already exists but the
    first ``match_supplier_rule`` call inside the learn path is forced to
    miss, so the INSERT collides with the committed row."""
    from saebooks.services import document_inbox as inbox_svc

    suffix = uuid.uuid4().hex[:8]
    vendor = f"Race Fuels {suffix}"
    extract_payload.update(vendor_name=vendor)
    doc = (await _upload(business_client)).json()
    assert doc["supplier_rule_id"] is None  # no rule at extraction time

    # The "winner": a committed rule in the exact unique scope the
    # learned rule would take (tenant, publish company, vendor_key).
    r = await _create_rule(
        business_client,
        deps,
        vendor_name=vendor,
        company_id=deps["company_id"],
    )
    assert r.status_code == 201, r.text
    winner_rule_id = r.json()["id"]

    real_match = inbox_svc.match_supplier_rule
    calls = {"n": 0}

    async def match_misses_once(*args: Any, **kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the race window: the concurrent insert not yet visible
        return await real_match(*args, **kwargs)

    monkeypatch.setattr(inbox_svc, "match_supplier_rule", match_misses_once)

    r = await _publish(
        business_client, doc["id"], _publish_body(deps, learn_rule=True)
    )
    assert r.status_code == 201, r.text
    assert calls["n"] >= 2  # miss → collide → re-match

    # Exactly one rule for the vendor — the winner's; nothing duplicated.
    items = (
        await business_client.get(
            "/api/v1/inbox/supplier-rules", params={"page_size": 200}
        )
    ).json()["items"]
    matching = [x for x in items if x["vendor_key"] == f"race fuels {suffix}"]
    assert len(matching) == 1
    assert matching[0]["id"] == winner_rule_id
