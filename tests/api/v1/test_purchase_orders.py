"""Phase 1 contract tests for /api/v1/purchase_orders.

Covers:
* Auth gate (401 without bearer)
* GET /api/v1/purchase_orders → 200 with pagination shape
* GET /api/v1/purchase_orders/{id} → 200 with lines; 404 on missing UUID
* POST /api/v1/purchase_orders → 201, version==1, change_log row created
* PATCH with correct If-Match → 200, version bumped
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* DELETE soft-archive (204 / 409 / 428)
* POST /{id}/send → DRAFT → OPEN, mints PO number
* POST /{id}/cancel → refuses if any received_qty > 0
* POST /{id}/convert-to-bill → default-full path produces a Bill and
  flips PO to RECEIVED; partial-quantities path flips to PARTIAL
* Multi-receipt safety: api_update rejects payloads that drop or
  regress a previously-received line's received_qty
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import ContactType
from saebooks.models.change_log import ChangeLog
from saebooks.models.contact import Contact


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture
async def unauth_client() -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
async def po_deps() -> dict[str, str]:
    """Return IDs needed to build a purchase-order payload.

    The AU CoA seeder loads accounts but no contacts; this fixture
    lazily provisions a SUPPLIER contact in the seed company so the
    PO suite is self-bootstrapping against a fresh test DB.
    """
    async with AsyncSessionLocal() as session:
        expense = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.EXPENSE,
                    Account.tenant_id == DEFAULT_TENANT_ID,
                ).limit(1)
            )
        ).scalars().first()
        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.archived_at.is_(None),
                    Contact.tenant_id == DEFAULT_TENANT_ID,
                    Contact.contact_type == ContactType.SUPPLIER,
                ).limit(1)
            )
        ).scalars().first()
        if contact is None:
            company = (
                await session.execute(
                    select(Company).where(
                        Company.tenant_id == DEFAULT_TENANT_ID,
                        Company.archived_at.is_(None),
                    ).limit(1)
                )
            ).scalars().first()
            assert company is not None, "Seed company missing — load_au_coa fixture broken"
            contact = Contact(
                tenant_id=DEFAULT_TENANT_ID,
                company_id=company.id,
                name="Test Vendor",
                contact_type=ContactType.SUPPLIER,
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)

    assert expense is not None, "Test DB has no EXPENSE account in default tenant"
    assert contact is not None, "Failed to provision test contact"
    return {
        "expense_account_id": str(expense.id),
        "contact_id": str(contact.id),
    }


def _po_payload(deps: dict[str, str], **overrides: object) -> dict:
    base: dict = {
        "contact_id": deps["contact_id"],
        "issue_date": "2026-04-01",
        "expected_date": "2026-04-15",
        "notes": "Test PO",
        "lines": [
            {
                "description": "Steel — 12mm plate",
                "account_id": deps["expense_account_id"],
                "quantity": "10",
                "unit_price": "50.00",
                "discount_pct": "0",
            },
            {
                "description": "Bolts — M12 x 50",
                "account_id": deps["expense_account_id"],
                "quantity": "100",
                "unit_price": "1.50",
                "discount_pct": "0",
            },
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_pos_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/purchase_orders")
    assert r.status_code == 401


async def test_pos_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/purchase_orders")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_pos_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/purchase_orders")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_pos_list_filter_by_status(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201

    r2 = await api_client.get(
        "/api/v1/purchase_orders", params={"status": "DRAFT"}
    )
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["status"] == "DRAFT"


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_pos_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/purchase_orders/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_pos_get_200_with_lines(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201, r.text
    po_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/purchase_orders/{po_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == po_id
    assert len(body["lines"]) == 2
    assert body["lines"][0]["description"] == "Steel — 12mm plate"
    # Each line's received_qty starts at 0
    assert all(float(ln["received_qty"]) == 0.0 for ln in body["lines"])


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_pos_create_201(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["status"] == "DRAFT"
    assert body["number"] is None  # number is minted on send, not on create
    assert "tenant_id" in body
    assert len(body["lines"]) == 2
    # Subtotal = 10*50 + 100*1.5 = 650.00
    assert float(body["subtotal"]) == 650.00


async def test_pos_create_change_log(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    """POST should produce a change_log row with op=create, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(po_id),
                    ChangeLog.entity == "purchase_order",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].op == "create"
    assert rows[0].version == 1


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_pos_update_bumps_version(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201, r.text
    po_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/purchase_orders/{po_id}",
        json={"notes": "Updated notes"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["notes"] == "Updated notes"


async def test_pos_update_requires_if_match(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/purchase_orders/{po_id}", json={"notes": "x"}
    )
    assert r2.status_code == 428


async def test_pos_stale_if_match_returns_409(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/purchase_orders/{po_id}",
        json={"notes": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == po_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Send (DRAFT → OPEN)
# ---------------------------------------------------------------------------


async def test_pos_send_mints_number_and_flips_open(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/send",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    sent = r2.json()
    assert sent["status"] == "OPEN"
    assert sent["number"] is not None
    assert sent["number"].startswith("PO-")
    assert sent["sent_at"] is not None
    assert sent["version"] == v + 1


# ---------------------------------------------------------------------------
# Cancel — refused after any receipt
# ---------------------------------------------------------------------------


async def test_pos_cancel_draft_ok(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/cancel",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "CANCELLED"


async def test_pos_cancel_after_receipt_refused(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    """Once any line has been (partially) received via convert-to-bill,
    cancel must be refused."""
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]
    v = r.json()["version"]

    # Send → OPEN
    r2 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/send",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200
    v = r2.json()["version"]

    # Convert partial — bill 5 of 10 on line 1 only
    r3 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/convert-to-bill",
        headers={"If-Match": str(v)},
        json={"quantities": {"1": "5"}},
    )
    assert r3.status_code == 200, r3.text
    po_after = r3.json()["purchase_order"]
    assert po_after["status"] == "PARTIAL"

    # Now cancel must refuse
    r4 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/cancel",
        headers={"If-Match": str(po_after["version"])},
    )
    assert r4.status_code == 422


# ---------------------------------------------------------------------------
# Convert-to-bill — full path
# ---------------------------------------------------------------------------


async def test_pos_convert_full_flips_received(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]
    v = r.json()["version"]

    # Send → OPEN
    r2 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/send",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200
    v = r2.json()["version"]

    # No quantities → bill everything
    r3 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/convert-to-bill",
        headers={"If-Match": str(v)},
        json={},
    )
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["bill_id"] is not None
    po = body["purchase_order"]
    assert po["status"] == "RECEIVED"
    # Every line fully received
    for ln in po["lines"]:
        assert float(ln["received_qty"]) == float(ln["quantity"])

    # Bill is fetchable & in DRAFT
    bill_id = body["bill_id"]
    r4 = await api_client.get(f"/api/v1/bills/{bill_id}")
    assert r4.status_code == 200
    bill = r4.json()
    assert bill["status"] == "DRAFT"
    assert len(bill["lines"]) == 2


async def test_pos_convert_partial_flips_partial(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/send",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200
    v = r2.json()["version"]

    # Bill only line 1, only 3 units
    r3 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/convert-to-bill",
        headers={"If-Match": str(v)},
        json={"quantities": {"1": "3"}},
    )
    assert r3.status_code == 200, r3.text
    po = r3.json()["purchase_order"]
    assert po["status"] == "PARTIAL"
    line1 = next(ln for ln in po["lines"] if ln["line_no"] == 1)
    line2 = next(ln for ln in po["lines"] if ln["line_no"] == 2)
    assert float(line1["received_qty"]) == 3.0
    assert float(line2["received_qty"]) == 0.0


# ---------------------------------------------------------------------------
# Multi-receipt safety: cannot regress received_qty on PATCH
# ---------------------------------------------------------------------------


async def test_pos_patch_cannot_drop_received_line(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]
    v = r.json()["version"]

    # Send + partial-receive line 1
    r2 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/send",
        headers={"If-Match": str(v)},
    )
    v = r2.json()["version"]
    r3 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/convert-to-bill",
        headers={"If-Match": str(v)},
        json={"quantities": {"1": "3"}},
    )
    v = r3.json()["purchase_order"]["version"]

    # PATCH dropping line 1 entirely (only sending line 2) → must 422
    r4 = await api_client.patch(
        f"/api/v1/purchase_orders/{po_id}",
        json={
            "lines": [
                {
                    "description": "Bolts — M12 x 50",
                    "account_id": po_deps["expense_account_id"],
                    "quantity": "100",
                    "unit_price": "1.50",
                    "discount_pct": "0",
                }
            ]
        },
        headers={"If-Match": str(v)},
    )
    assert r4.status_code == 422


# ---------------------------------------------------------------------------
# Delete (soft-archive) → 204
# ---------------------------------------------------------------------------


async def test_pos_archive_204(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/purchase_orders/{po_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    r3 = await api_client.get("/api/v1/purchase_orders")
    ids = [i["id"] for i in r3.json()["items"]]
    assert po_id not in ids


async def test_pos_delete_stale_if_match_409(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/purchase_orders/{po_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_pos_delete_requires_if_match(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/purchase_orders/{po_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log full sequence: create + send + convert + archive
# ---------------------------------------------------------------------------


async def test_pos_change_log_create_send_archive(
    api_client: AsyncClient, po_deps: dict[str, str]
) -> None:
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/purchase_orders", json=_po_payload(po_deps))
    assert r.status_code == 201
    po_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/purchase_orders/{po_id}/send",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200
    v = r2.json()["version"]

    r3 = await api_client.delete(
        f"/api/v1/purchase_orders/{po_id}",
        headers={"If-Match": str(v)},
    )
    assert r3.status_code == 204

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(po_id),
                    ChangeLog.entity == "purchase_order",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert [row.op for row in rows] == ["create", "update", "archive"]
    assert [row.version for row in rows] == [1, 2, 3]
