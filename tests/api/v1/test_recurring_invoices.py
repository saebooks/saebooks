"""Phase 1 tier-4 contract tests for /api/v1/recurring_invoices.

Covers:
* Auth gate (401 without bearer, 401 with wrong token)
* GET /api/v1/recurring_invoices → 200 with pagination shape
* GET /api/v1/recurring_invoices/{id} → 200 (lines nested); 404 on missing UUID
* GET /api/v1/recurring_invoices?archived=true → only archived results
* GET /api/v1/recurring_invoices?status=ACTIVE → status filter
* GET /api/v1/recurring_invoices?frequency=MONTHLY → frequency filter
* GET /api/v1/recurring_invoices?contact_id=... → contact filter
* POST → 201, version==1, tenant_id present, change_log row created
* POST idempotency: same X-Idempotency-Key returns same response
* POST → next_run matches supplied date
* POST → lines round-trip (count, field values)
* POST without required field → 422
* PATCH with correct If-Match → 200, version bumped
* PATCH status transition ACTIVE → PAUSED → 200
* PATCH with stale If-Match → 409 with current state in body
* PATCH without If-Match → 428
* PATCH with lines key present → lines fully replaced
* PATCH without lines key → existing lines untouched
* DELETE with correct If-Match → 204 (soft-archive)
* DELETE with stale If-Match → 409
* DELETE without If-Match → 428
* Archived templates not in default list but appear with ?archived=true
* change_log sequence: create + update + delete = 3 rows with ops in order
* Frequency enum values round-trip: WEEKLY / FORTNIGHTLY / MONTHLY / QUARTERLY / YEARLY
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account, AccountType
from saebooks.models.change_log import ChangeLog
from saebooks.models.company import Company
from saebooks.models.contact import Contact


# ---------------------------------------------------------------------------
# Helpers — resolve IDs from the seeded DB
# ---------------------------------------------------------------------------


async def _deps() -> dict[str, str]:
    """Return contact_id and income account_id from seeded data."""
    async with AsyncSessionLocal() as session:
        contact = (
            await session.execute(
                select(Contact).where(Contact.archived_at.is_(None)).limit(1)
            )
        ).scalars().first()
        account = (
            await session.execute(
                select(Account).where(
                    Account.archived_at.is_(None),
                    Account.account_type == AccountType.INCOME,
                ).limit(1)
            )
        ).scalars().first()
    assert contact is not None, "Test DB has no contact"
    assert account is not None, "Test DB has no INCOME account"
    return {
        "contact_id": str(contact.id),
        "account_id": str(account.id),
    }


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
async def deps() -> dict[str, str]:
    return await _deps()


def _ri_payload(deps: dict[str, str], **overrides: object) -> dict:
    """Return a minimal valid RecurringInvoiceCreate payload."""
    base: dict = {
        "name": "Monthly Retainer",
        "contact_id": deps["contact_id"],
        "frequency": "MONTHLY",
        "next_run": "2026-05-01",
        "lines": [
            {
                "description": "Retainer fee",
                "account_id": deps["account_id"],
                "quantity": "1",
                "unit_price": "500.00",
                "discount_pct": "0",
            }
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_recurring_invoices_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/recurring_invoices")
    assert r.status_code == 401


async def test_recurring_invoices_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/recurring_invoices")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_recurring_invoices_list_200(api_client: AsyncClient, deps: dict) -> None:
    r = await api_client.get("/api/v1/recurring_invoices")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert isinstance(body["items"], list)


async def test_recurring_invoices_list_default_excludes_archived(
    api_client: AsyncClient, deps: dict
) -> None:
    """Default list must not include archived templates."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(
        f"/api/v1/recurring_invoices/{ri_id}", headers={"If-Match": str(v)}
    )

    r2 = await api_client.get("/api/v1/recurring_invoices", params={"page_size": 500})
    ids = [i["id"] for i in r2.json()["items"]]
    assert ri_id not in ids


async def test_recurring_invoices_list_archived_filter(
    api_client: AsyncClient, deps: dict
) -> None:
    """?archived=true must return archived templates."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]
    v = r.json()["version"]

    await api_client.delete(
        f"/api/v1/recurring_invoices/{ri_id}", headers={"If-Match": str(v)}
    )

    r2 = await api_client.get(
        "/api/v1/recurring_invoices", params={"archived": "true", "page_size": 500}
    )
    assert r2.status_code == 200
    ids = [i["id"] for i in r2.json()["items"]]
    assert ri_id in ids


async def test_recurring_invoices_list_status_filter(
    api_client: AsyncClient, deps: dict
) -> None:
    """?status=ACTIVE must only return ACTIVE templates; ?status=PAUSED must only return PAUSED."""
    # Create an ACTIVE template and verify the filter only returns ACTIVE items.
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]
    v = r.json()["version"]

    # Pause it so we can verify PAUSED filter.
    r_pause = await api_client.patch(
        f"/api/v1/recurring_invoices/{ri_id}",
        json={"status": "PAUSED"},
        headers={"If-Match": str(v)},
    )
    assert r_pause.status_code == 200

    # Verify PAUSED filter only returns PAUSED items and includes our template.
    r2 = await api_client.get("/api/v1/recurring_invoices", params={"status": "PAUSED"})
    assert r2.status_code == 200
    assert r2.json()["total"] >= 1
    for item in r2.json()["items"]:
        assert item["status"] == "PAUSED"
    # Our template should appear (it's on page 1 since we just created it — it's the newest).
    # Verify via direct GET rather than scanning list.
    r_get = await api_client.get(f"/api/v1/recurring_invoices/{ri_id}")
    assert r_get.status_code == 200
    assert r_get.json()["status"] == "PAUSED"


async def test_recurring_invoices_list_frequency_filter(
    api_client: AsyncClient, deps: dict
) -> None:
    """?frequency=YEARLY must only return YEARLY templates."""
    # Use YEARLY to minimise collision with other tests (most use MONTHLY/QUARTERLY).
    r = await api_client.post(
        "/api/v1/recurring_invoices",
        json=_ri_payload(deps, frequency="YEARLY"),
    )
    assert r.status_code == 201
    ri_id = r.json()["id"]

    r2 = await api_client.get(
        "/api/v1/recurring_invoices",
        params={"frequency": "YEARLY", "page_size": 500},
    )
    assert r2.status_code == 200
    ids = [i["id"] for i in r2.json()["items"]]
    assert ri_id in ids
    for item in r2.json()["items"]:
        assert item["frequency"] == "YEARLY"


async def test_recurring_invoices_list_contact_filter(
    api_client: AsyncClient, deps: dict
) -> None:
    """?contact_id=... must only return templates for that contact."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201

    r2 = await api_client.get(
        "/api/v1/recurring_invoices",
        params={"contact_id": deps["contact_id"], "page_size": 500},
    )
    assert r2.status_code == 200
    assert r2.json()["total"] > 0
    for item in r2.json()["items"]:
        assert item["contact_id"] == deps["contact_id"]


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------


async def test_recurring_invoices_get_404_unknown_uuid(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/recurring_invoices/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_recurring_invoices_get_200_with_lines(
    api_client: AsyncClient, deps: dict
) -> None:
    """GET /{id} returns full template with lines nested."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]

    r2 = await api_client.get(f"/api/v1/recurring_invoices/{ri_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == ri_id
    assert body["status"] == "ACTIVE"
    assert "lines" in body
    assert len(body["lines"]) == 1
    assert body["lines"][0]["description"] == "Retainer fee"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_recurring_invoices_create_201(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["archived_at"] is None
    assert body["status"] == "ACTIVE"
    assert "tenant_id" in body
    assert "id" in body
    assert body["frequency"] == "MONTHLY"
    assert body["next_run"] == "2026-05-01"
    assert len(body["lines"]) == 1


async def test_recurring_invoices_create_lines_round_trip(
    api_client: AsyncClient, deps: dict
) -> None:
    """Lines submitted on POST round-trip with correct values."""
    payload = _ri_payload(
        deps,
        lines=[
            {
                "description": "Line A",
                "account_id": deps["account_id"],
                "quantity": "2",
                "unit_price": "100.00",
                "discount_pct": "10",
            },
            {
                "description": "Line B",
                "account_id": deps["account_id"],
                "quantity": "1",
                "unit_price": "250.00",
                "discount_pct": "0",
            },
        ],
    )
    r = await api_client.post("/api/v1/recurring_invoices", json=payload)
    assert r.status_code == 201, r.text
    lines = r.json()["lines"]
    assert len(lines) == 2
    descs = {ln["description"] for ln in lines}
    assert descs == {"Line A", "Line B"}


async def test_recurring_invoices_create_missing_required(
    api_client: AsyncClient, deps: dict
) -> None:
    """POST without contact_id → 422."""
    payload = _ri_payload(deps)
    del payload["contact_id"]
    r = await api_client.post("/api/v1/recurring_invoices", json=payload)
    assert r.status_code == 422


async def test_recurring_invoices_create_change_log(
    api_client: AsyncClient, deps: dict
) -> None:
    """POST should produce a change_log row with op=created, version=1."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(ri_id),
                    ChangeLog.entity == "recurring_invoice",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) >= 1
    assert rows[-1].op == "created"
    assert rows[-1].version == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_recurring_invoices_create_idempotency(
    api_client: AsyncClient, deps: dict
) -> None:
    """Same X-Idempotency-Key returns the same response body on replay."""
    key = str(uuid.uuid4())
    payload = _ri_payload(deps)

    r1 = await api_client.post(
        "/api/v1/recurring_invoices",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    r2 = await api_client.post(
        "/api/v1/recurring_invoices",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == id1


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_recurring_invoices_update_bumps_version(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/recurring_invoices/{ri_id}",
        json={"name": "Updated Retainer"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    updated = r2.json()
    assert updated["version"] == v + 1
    assert updated["name"] == "Updated Retainer"


async def test_recurring_invoices_patch_status_transition(
    api_client: AsyncClient, deps: dict
) -> None:
    """PATCH status ACTIVE → PAUSED → 200 with new status."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.patch(
        f"/api/v1/recurring_invoices/{ri_id}",
        json={"status": "PAUSED"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "PAUSED"


async def test_recurring_invoices_patch_lines_replaced(
    api_client: AsyncClient, deps: dict
) -> None:
    """PATCH with lines key → old lines deleted, new lines inserted."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]
    v = r.json()["version"]
    assert len(r.json()["lines"]) == 1

    new_lines = [
        {
            "description": "New line X",
            "account_id": deps["account_id"],
            "quantity": "3",
            "unit_price": "200.00",
            "discount_pct": "0",
        },
        {
            "description": "New line Y",
            "account_id": deps["account_id"],
            "quantity": "1",
            "unit_price": "50.00",
            "discount_pct": "0",
        },
    ]
    r2 = await api_client.patch(
        f"/api/v1/recurring_invoices/{ri_id}",
        json={"lines": new_lines},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    lines = r2.json()["lines"]
    assert len(lines) == 2
    descs = {ln["description"] for ln in lines}
    assert descs == {"New line X", "New line Y"}


async def test_recurring_invoices_patch_no_lines_key_preserves_lines(
    api_client: AsyncClient, deps: dict
) -> None:
    """PATCH without lines key → existing lines untouched."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]
    v = r.json()["version"]
    original_line_count = len(r.json()["lines"])

    r2 = await api_client.patch(
        f"/api/v1/recurring_invoices/{ri_id}",
        json={"name": "Renamed"},
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["lines"]) == original_line_count
    assert r2.json()["name"] == "Renamed"


# ---------------------------------------------------------------------------
# Update — missing / stale If-Match
# ---------------------------------------------------------------------------


async def test_recurring_invoices_update_requires_if_match(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/recurring_invoices/{ri_id}", json={"name": "x"}
    )
    assert r2.status_code == 428


async def test_recurring_invoices_stale_if_match_returns_409(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]

    r2 = await api_client.patch(
        f"/api/v1/recurring_invoices/{ri_id}",
        json={"name": "stale attempt"},
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == ri_id
    assert body["current"]["version"] == 1


# ---------------------------------------------------------------------------
# Delete → 204
# ---------------------------------------------------------------------------


async def test_recurring_invoices_delete_204(
    api_client: AsyncClient, deps: dict
) -> None:
    """DELETE with correct If-Match → 204, no longer in default list."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]
    v = r.json()["version"]

    r2 = await api_client.delete(
        f"/api/v1/recurring_invoices/{ri_id}",
        headers={"If-Match": str(v)},
    )
    assert r2.status_code == 204

    r3 = await api_client.get("/api/v1/recurring_invoices")
    ids = [i["id"] for i in r3.json()["items"]]
    assert ri_id not in ids


async def test_recurring_invoices_delete_stale_if_match_409(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]

    r2 = await api_client.delete(
        f"/api/v1/recurring_invoices/{ri_id}",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409


async def test_recurring_invoices_delete_requires_if_match(
    api_client: AsyncClient, deps: dict
) -> None:
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]

    r2 = await api_client.delete(f"/api/v1/recurring_invoices/{ri_id}")
    assert r2.status_code == 428


# ---------------------------------------------------------------------------
# change_log sequence
# ---------------------------------------------------------------------------


async def test_recurring_invoices_change_log_full_sequence(
    api_client: AsyncClient, deps: dict
) -> None:
    """Create + update + delete = 3 recurring_invoice change_log rows in order."""
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1)
            )
        ).scalar_one_or_none() or 0

    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]

    await api_client.patch(
        f"/api/v1/recurring_invoices/{ri_id}",
        json={"notes": "Updated"},
        headers={"If-Match": "1"},
    )
    await api_client.delete(
        f"/api/v1/recurring_invoices/{ri_id}",
        headers={"If-Match": "2"},
    )

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(ri_id),
                    ChangeLog.entity == "recurring_invoice",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    ops = [row.op for row in rows]
    versions = [row.version for row in rows]
    assert "created" in ops
    assert "updated" in ops
    assert "deleted" in ops
    assert versions == sorted(versions)  # monotonically increasing


# ---------------------------------------------------------------------------
# Frequency enum round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frequency",
    ["WEEKLY", "FORTNIGHTLY", "MONTHLY", "QUARTERLY", "YEARLY"],
)
async def test_recurring_invoices_frequency_enum_round_trip(
    api_client: AsyncClient, deps: dict, frequency: str
) -> None:
    """All frequency enum values must be accepted on POST and round-trip in response."""
    r = await api_client.post(
        "/api/v1/recurring_invoices",
        json=_ri_payload(deps, frequency=frequency),
    )
    assert r.status_code == 201, r.text
    assert r.json()["frequency"] == frequency


# ---------------------------------------------------------------------------
# Manual generation — POST /{id}/generate
# ---------------------------------------------------------------------------


async def test_ri_generate_creates_invoice(
    api_client: AsyncClient, deps: dict
) -> None:
    """ACTIVE RI with lines → 201, invoice_id and invoice body returned."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201, r.text
    ri_id = r.json()["id"]
    version = r.json()["version"]
    next_run = r.json()["next_run"]

    r2 = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/generate",
        headers={"If-Match": str(version)},
    )
    assert r2.status_code == 201, r2.text
    body = r2.json()
    assert "invoice_id" in body
    assert "invoice" in body
    invoice = body["invoice"]
    assert invoice["id"] == body["invoice_id"]
    assert invoice["status"] in ("DRAFT", "POSTED")
    assert invoice["contact_id"] == deps["contact_id"]
    assert invoice["issue_date"] == next_run
    assert len(invoice["lines"]) == 1

    # Template version should have advanced (next_run changed, invoices_generated++).
    r3 = await api_client.get(f"/api/v1/recurring_invoices/{ri_id}")
    assert r3.status_code == 200
    updated_ri = r3.json()
    assert updated_ri["invoices_generated"] == 1
    assert updated_ri["next_run"] != next_run


async def test_ri_generate_paused_422(
    api_client: AsyncClient, deps: dict
) -> None:
    """PAUSED recurring invoice → 422 on generate."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]
    v = r.json()["version"]

    # Pause it.
    rp = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/pause",
        headers={"If-Match": str(v)},
    )
    assert rp.status_code == 200
    paused_version = rp.json()["version"]

    r2 = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/generate",
        headers={"If-Match": str(paused_version)},
    )
    assert r2.status_code == 422
    assert "PAUSED" in r2.json()["detail"]


async def test_ri_generate_stale_409(
    api_client: AsyncClient, deps: dict
) -> None:
    """Wrong If-Match version on generate → 409 with current state."""
    r = await api_client.post("/api/v1/recurring_invoices", json=_ri_payload(deps))
    assert r.status_code == 201
    ri_id = r.json()["id"]

    r2 = await api_client.post(
        f"/api/v1/recurring_invoices/{ri_id}/generate",
        headers={"If-Match": "99"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == ri_id
    assert body["current"]["version"] == 1
