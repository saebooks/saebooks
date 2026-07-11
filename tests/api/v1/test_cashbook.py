"""HTTP contract tests for /api/v1/cashbook/*.

Covers Phase B routing/schema/projection — the deep service invariants
(trial-balance, GST 2-vs-3 line, idempotency replay) live in
``tests/services/test_cashbook.py`` and aren't re-tested here.

Coverage:
- Auth gate (401 without bearer)
- POST /entries 201 with idempotency key
- POST /entries 400 when X-Idempotency-Key missing
- POST /entries 400 when category unknown / wrong direction
- POST /entries 409 when company is not in cashbook mode
- GET /entries lists cashbook-tagged JEs only (non-cashbook JEs filtered)
- GET /entries filters by direction / category / date range
- GET /entries/{id} returns 404 for non-cashbook JE
- GET /categories renders default picker; per-company overrides applied
- GET /summary aggregates income/expense/by_category from cashbook_meta
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.services import settings as settings_svc

pytestmark = pytest.mark.postgres_only


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


async def _seed_company_into_cashbook_mode(
    *,
    gst_registered: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Flip the test seed company into cashbook mode. Returns
    ``(tenant_id, company_id)``. Mirrors the helper used by the service
    tests — same shape so the API tests pick up the same configuration.
    """
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None, "seed company missing — check conftest seed_coa"

        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == co.id,
                    Account.code == "1-1110",
                )
            )
        ).scalar_one_or_none()
        assert bank is not None, "AU CoA seed missing 1-1110 Bank"

        co.bookkeeping_mode = "cashbook"
        co.cashbook_default_bank_account_id = bank.id
        co.gst_registered = gst_registered
        co.cashbook_categories = None  # reset overrides between tests

        if gst_registered:
            await settings_svc.set(session, "gst_collected_account_code", "2-1310")
            await settings_svc.set(session, "gst_paid_account_code", "2-1330")
            await settings_svc.set(session, "gst_auto_post", "true")
        await session.commit()
        return co.tenant_id, co.id


async def _set_company_overrides(overrides: dict | None) -> None:
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        co.cashbook_categories = overrides
        await session.commit()


async def _reset_company_to_full_mode() -> None:
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        co.bookkeeping_mode = "full"
        co.cashbook_default_bank_account_id = None
        co.cashbook_categories = None
        await session.commit()


def _new_key(prefix: str = "api") -> str:
    return f"{prefix}-{uuid.uuid4()}"


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


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_unauth_requests_rejected(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/cashbook/categories")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /entries
# ---------------------------------------------------------------------------


async def test_create_entry_201_returns_cashbook_shape(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    body = {
        "entry_date": "2026-05-08",
        "description": "Bunnings — drill bits",
        "amount": "120.50",
        "direction": "expense",
        "category_code": "EXP_TOOLS",
    }
    r = await api_client.post(
        "/api/v1/cashbook/entries",
        json=body,
        headers={"X-Idempotency-Key": _new_key()},
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["category_code"] == "EXP_TOOLS"
    assert out["direction"] == "expense"
    assert out["amount"] == "120.50"
    assert out["status"] in ("EntryStatus.POSTED", "POSTED", "posted")
    assert out["journal_entry_id"]
    assert out["journal_entry_ref"].startswith("JE-")


async def test_create_entry_idempotency_replay(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    key = _new_key()
    body = {
        "entry_date": "2026-05-08",
        "description": "Idem replay",
        "amount": "55.00",
        "direction": "expense",
        "category_code": "EXP_OTHER",
    }
    r1 = await api_client.post(
        "/api/v1/cashbook/entries", json=body, headers={"X-Idempotency-Key": key}
    )
    r2 = await api_client.post(
        "/api/v1/cashbook/entries", json=body, headers={"X-Idempotency-Key": key}
    )
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["journal_entry_id"] == r2.json()["journal_entry_id"]


async def test_create_entry_missing_idempotency_key_400(
    api_client: AsyncClient,
) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    body = {
        "entry_date": "2026-05-08",
        "description": "no key",
        "amount": "10.00",
        "direction": "expense",
        "category_code": "EXP_OTHER",
    }
    r = await api_client.post("/api/v1/cashbook/entries", json=body)
    assert r.status_code == 400
    detail = r.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "idempotency_key_required"


async def test_create_entry_unknown_category_400(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    body = {
        "entry_date": "2026-05-08",
        "description": "bad cat",
        "amount": "10.00",
        "direction": "expense",
        "category_code": "EXP_DOES_NOT_EXIST",
    }
    r = await api_client.post(
        "/api/v1/cashbook/entries",
        json=body,
        headers={"X-Idempotency-Key": _new_key()},
    )
    assert r.status_code == 400
    detail = r.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "cashbook_category_invalid"


async def test_create_entry_wrong_direction_400(api_client: AsyncClient) -> None:
    """Income category submitted on the expense side."""
    await _seed_company_into_cashbook_mode(gst_registered=False)
    body = {
        "entry_date": "2026-05-08",
        "description": "income code on expense side",
        "amount": "100.00",
        "direction": "expense",
        "category_code": "INC_SALES",
    }
    r = await api_client.post(
        "/api/v1/cashbook/entries",
        json=body,
        headers={"X-Idempotency-Key": _new_key()},
    )
    assert r.status_code == 400
    detail = r.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "cashbook_category_invalid"


async def test_create_entry_company_not_in_cashbook_mode_409(
    api_client: AsyncClient,
) -> None:
    """Company in 'full' mode → 409 cashbook_not_configured."""
    await _reset_company_to_full_mode()
    try:
        body = {
            "entry_date": "2026-05-08",
            "description": "wrong mode",
            "amount": "10.00",
            "direction": "expense",
            "category_code": "EXP_OTHER",
        }
        r = await api_client.post(
            "/api/v1/cashbook/entries",
            json=body,
            headers={"X-Idempotency-Key": _new_key()},
        )
        assert r.status_code == 409
        detail = r.json().get("detail")
        assert (
            isinstance(detail, dict)
            and detail.get("code") == "cashbook_not_configured"
        )
    finally:
        # Restore cashbook mode for any later test in the session.
        await _seed_company_into_cashbook_mode(gst_registered=False)


# ---------------------------------------------------------------------------
# Negative payload validation (Pydantic)
# ---------------------------------------------------------------------------


async def test_create_entry_zero_amount_422(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    body = {
        "entry_date": "2026-05-08",
        "description": "zero",
        "amount": "0",
        "direction": "expense",
        "category_code": "EXP_OTHER",
    }
    r = await api_client.post(
        "/api/v1/cashbook/entries",
        json=body,
        headers={"X-Idempotency-Key": _new_key()},
    )
    assert r.status_code == 422


async def test_create_entry_bad_direction_422(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    body = {
        "entry_date": "2026-05-08",
        "description": "bad direction",
        "amount": "10",
        "direction": "transfer",  # not allowed by the route schema
        "category_code": "EXP_OTHER",
    }
    r = await api_client.post(
        "/api/v1/cashbook/entries",
        json=body,
        headers={"X-Idempotency-Key": _new_key()},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /entries
# ---------------------------------------------------------------------------


async def test_list_entries_returns_cashbook_only(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)

    # Create two cashbook entries.
    for i in range(2):
        body = {
            "entry_date": "2026-05-08",
            "description": f"list test {i}",
            "amount": "11.11",
            "direction": "expense",
            "category_code": "EXP_OTHER",
        }
        r = await api_client.post(
            "/api/v1/cashbook/entries",
            json=body,
            headers={"X-Idempotency-Key": _new_key("list")},
        )
        assert r.status_code == 201, r.text

    r = await api_client.get("/api/v1/cashbook/entries")
    assert r.status_code == 200, r.text
    out = r.json()
    assert "items" in out and "next_cursor" in out
    # Every returned row must be cashbook-tagged.
    for item in out["items"]:
        assert item["category_code"]
        assert item["direction"] in ("income", "expense")


async def test_list_entries_filters_direction(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)

    inc_body = {
        "entry_date": "2026-05-08",
        "description": "income filter",
        "amount": "200.00",
        "direction": "income",
        "category_code": "INC_SALES",
    }
    exp_body = {
        "entry_date": "2026-05-08",
        "description": "expense filter",
        "amount": "30.00",
        "direction": "expense",
        "category_code": "EXP_OTHER",
    }
    for body in (inc_body, exp_body):
        r = await api_client.post(
            "/api/v1/cashbook/entries",
            json=body,
            headers={"X-Idempotency-Key": _new_key("dir")},
        )
        assert r.status_code == 201, r.text

    r = await api_client.get("/api/v1/cashbook/entries", params={"direction": "income"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert items, "expected at least one income row"
    assert all(it["direction"] == "income" for it in items)


async def test_list_entries_filters_category(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    body_a = {
        "entry_date": "2026-05-08",
        "description": "tools",
        "amount": "20.00",
        "direction": "expense",
        "category_code": "EXP_TOOLS",
    }
    body_b = {
        "entry_date": "2026-05-08",
        "description": "vehicle",
        "amount": "44.00",
        "direction": "expense",
        "category_code": "EXP_VEHICLE",
    }
    for body in (body_a, body_b):
        r = await api_client.post(
            "/api/v1/cashbook/entries",
            json=body,
            headers={"X-Idempotency-Key": _new_key("cat")},
        )
        assert r.status_code == 201
    r = await api_client.get(
        "/api/v1/cashbook/entries", params={"category": "EXP_TOOLS"}
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert items, "expected at least one EXP_TOOLS row"
    assert all(it["category_code"] == "EXP_TOOLS" for it in items)


async def test_get_entry_returns_cashbook_shape(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    body = {
        "entry_date": "2026-05-08",
        "description": "single",
        "amount": "33.00",
        "direction": "expense",
        "category_code": "EXP_OTHER",
    }
    r = await api_client.post(
        "/api/v1/cashbook/entries",
        json=body,
        headers={"X-Idempotency-Key": _new_key()},
    )
    entry_id = r.json()["id"]
    r2 = await api_client.get(f"/api/v1/cashbook/entries/{entry_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == entry_id


async def test_get_entry_unknown_404(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    r = await api_client.get(f"/api/v1/cashbook/entries/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /categories
# ---------------------------------------------------------------------------


async def test_categories_endpoint_returns_full_picker(
    api_client: AsyncClient,
) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    await _set_company_overrides(None)
    r = await api_client.get("/api/v1/cashbook/categories")
    assert r.status_code == 200
    cats = r.json()
    codes = {c["code"] for c in cats}
    # All 20 default codes must be present.
    expected = {
        "INC_SALES", "INC_SERVICES", "INC_INTEREST", "INC_OTHER",
        "EXP_VEHICLE", "EXP_HOME_OFFICE", "EXP_INSURANCE", "EXP_PROFESSIONAL",
        "EXP_MATERIALS", "EXP_SOFTWARE", "EXP_TELCO", "EXP_SUPER",
        "EXP_TRAINING", "EXP_TOOLS", "EXP_TRAVEL", "EXP_BANK", "EXP_OTHER",
        "CAP_PURCHASE", "PER_DRAWINGS", "TX_TRANSFER",
    }
    assert expected <= codes


async def test_categories_label_override_applied(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    await _set_company_overrides(
        {"version": 1, "overrides": {"EXP_VEHICLE": {"label": "Ute & fuel"}}}
    )
    try:
        r = await api_client.get("/api/v1/cashbook/categories")
        assert r.status_code == 200
        veh = next(c for c in r.json() if c["code"] == "EXP_VEHICLE")
        assert veh["label"] == "Ute & fuel"
    finally:
        await _set_company_overrides(None)


async def test_categories_hidden_dropped(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    await _set_company_overrides(
        {"version": 1, "overrides": {"INC_INTEREST": {"hidden": True}}}
    )
    try:
        r = await api_client.get("/api/v1/cashbook/categories")
        assert r.status_code == 200
        codes = {c["code"] for c in r.json()}
        assert "INC_INTEREST" not in codes
    finally:
        await _set_company_overrides(None)


# ---------------------------------------------------------------------------
# GET /summary
# ---------------------------------------------------------------------------


async def test_summary_aggregates_income_expense(api_client: AsyncClient) -> None:
    """Summary aggregates correctly. Asserted as deltas so any DB state
    left by prior runs (the dev DB persists across pytest sessions) is
    tolerated — what the test owns is the *change* it caused."""
    await _seed_company_into_cashbook_mode(gst_registered=False)

    # Future date — no other test currently writes in 2099-Q1.
    target_date = "2099-01-15"
    range_params = {"from": "2099-01-01", "to": "2099-01-31"}

    from decimal import Decimal as _D

    # Snapshot the current summary so we can assert deltas.
    pre = (await api_client.get("/api/v1/cashbook/summary", params=range_params)).json()

    posts = [
        {
            "entry_date": target_date, "description": "sale",
            "amount": "1000.00", "direction": "income",
            "category_code": "INC_SALES",
        },
        {
            "entry_date": target_date, "description": "fuel",
            "amount": "100.00", "direction": "expense",
            "category_code": "EXP_VEHICLE",
        },
        {
            "entry_date": target_date, "description": "tools",
            "amount": "50.00", "direction": "expense",
            "category_code": "EXP_TOOLS",
        },
    ]
    for body in posts:
        r = await api_client.post(
            "/api/v1/cashbook/entries",
            json=body,
            headers={"X-Idempotency-Key": _new_key("sum")},
        )
        assert r.status_code == 201, r.text

    r = await api_client.get("/api/v1/cashbook/summary", params=range_params)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["from"] == "2099-01-01"
    assert out["to"] == "2099-01-31"

    # Non-registered → no GST split, so amount-net == amount. Assert
    # the deltas.
    assert _D(out["income_total"]) - _D(pre["income_total"]) == _D("1000.00")
    assert _D(out["expense_total"]) - _D(pre["expense_total"]) == _D("150.00")
    assert _D(out["net"]) - _D(pre["net"]) == _D("850.00")
    assert _D(out["gst_collected"]) == _D("0")
    assert _D(out["gst_paid"]) == _D("0")

    by_cat = {c["code"]: c for c in out["by_category"]}
    assert "INC_SALES" in by_cat
    # by_category is cumulative within the range — assert at least our
    # contribution shows up (count >= 1) rather than == 1.
    assert by_cat["INC_SALES"]["count"] >= 1


async def test_summary_bad_range_400(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    r = await api_client.get(
        "/api/v1/cashbook/summary",
        params={"from": "2099-01-31", "to": "2099-01-01"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /entries/{id}  (soft-delete)
# ---------------------------------------------------------------------------


async def _create_entry(api_client: AsyncClient, **overrides) -> dict:
    body = {
        "entry_date": "2026-05-08",
        "description": "patch/delete test",
        "amount": "42.00",
        "direction": "expense",
        "category_code": "EXP_OTHER",
    }
    body.update(overrides)
    r = await api_client.post(
        "/api/v1/cashbook/entries",
        json=body,
        headers={"X-Idempotency-Key": _new_key("del-create")},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_delete_entry_returns_204_and_hides_from_list(
    api_client: AsyncClient,
) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    created = await _create_entry(api_client)
    entry_id = created["id"]

    r = await api_client.delete(f"/api/v1/cashbook/entries/{entry_id}")
    assert r.status_code == 204

    # GET-by-id returns 404
    r2 = await api_client.get(f"/api/v1/cashbook/entries/{entry_id}")
    assert r2.status_code == 404

    # List by category — voided entry is filtered.
    r3 = await api_client.get(
        "/api/v1/cashbook/entries", params={"category": "EXP_OTHER"}
    )
    assert r3.status_code == 200
    assert all(item["id"] != entry_id for item in r3.json()["items"])


async def test_delete_entry_idempotent(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    created = await _create_entry(api_client)
    entry_id = created["id"]

    r1 = await api_client.delete(f"/api/v1/cashbook/entries/{entry_id}")
    r2 = await api_client.delete(f"/api/v1/cashbook/entries/{entry_id}")
    assert r1.status_code == 204
    assert r2.status_code == 204


async def test_delete_unknown_entry_404(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    r = await api_client.delete(f"/api/v1/cashbook/entries/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /entries/{id}  (void + recreate)
# ---------------------------------------------------------------------------


async def test_patch_entry_creates_replacement_with_link(
    api_client: AsyncClient,
) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    created = await _create_entry(
        api_client,
        amount="100.00",
        description="original payload",
        category_code="EXP_OTHER",
    )
    original_id = created["id"]

    new_payload = {
        "entry_date": "2026-05-08",
        "description": "new payload",
        "amount": "250.00",
        "direction": "expense",
        "category_code": "EXP_TOOLS",
    }
    r = await api_client.patch(
        f"/api/v1/cashbook/entries/{original_id}",
        json=new_payload,
        headers={"X-Idempotency-Key": _new_key("patch")},
    )
    assert r.status_code == 200, r.text
    new_entry = r.json()
    assert new_entry["id"] != original_id
    assert new_entry["amount"] == "250.00"
    assert new_entry["category_code"] == "EXP_TOOLS"

    # Original is hidden from cashbook surfaces.
    r2 = await api_client.get(f"/api/v1/cashbook/entries/{original_id}")
    assert r2.status_code == 404

    # New is visible.
    r3 = await api_client.get(f"/api/v1/cashbook/entries/{new_entry['id']}")
    assert r3.status_code == 200


async def test_patch_entry_idempotent_on_replay(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    created = await _create_entry(api_client)
    original_id = created["id"]
    key = _new_key("patch-idem")
    body = {
        "entry_date": "2026-05-08",
        "description": "replay",
        "amount": "55.00",
        "direction": "expense",
        "category_code": "EXP_OTHER",
    }
    r1 = await api_client.patch(
        f"/api/v1/cashbook/entries/{original_id}",
        json=body,
        headers={"X-Idempotency-Key": key},
    )
    r2 = await api_client.patch(
        f"/api/v1/cashbook/entries/{original_id}",
        json=body,
        headers={"X-Idempotency-Key": key},
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


async def test_patch_entry_missing_idempotency_key_400(
    api_client: AsyncClient,
) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    created = await _create_entry(api_client)
    body = {
        "entry_date": "2026-05-08",
        "description": "no key",
        "amount": "11.00",
        "direction": "expense",
        "category_code": "EXP_OTHER",
    }
    r = await api_client.patch(
        f"/api/v1/cashbook/entries/{created['id']}", json=body
    )
    assert r.status_code == 400
    detail = r.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "idempotency_key_required"


async def test_patch_unknown_entry_404(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    body = {
        "entry_date": "2026-05-08",
        "description": "no entry",
        "amount": "11.00",
        "direction": "expense",
        "category_code": "EXP_OTHER",
    }
    r = await api_client.patch(
        f"/api/v1/cashbook/entries/{uuid.uuid4()}",
        json=body,
        headers={"X-Idempotency-Key": _new_key("patch-404")},
    )
    assert r.status_code == 404
    detail = r.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "cashbook_entry_not_found"


# ---------------------------------------------------------------------------
# Phase C — onboarding (POST /setup) + upgrade (POST /upgrade-to-full)
# ---------------------------------------------------------------------------


async def _bank_account_id() -> uuid.UUID:
    """Return the seeded 1-1110 Bank account id for the seed company."""
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None
        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == co.id,
                    Account.code == "1-1110",
                )
            )
        ).scalar_one()
        return bank.id


async def test_setup_idempotent_when_already_cashbook(api_client: AsyncClient) -> None:
    """Re-running /setup on a cashbook company just re-pins the bank
    account — no error, version still bumps."""
    await _seed_company_into_cashbook_mode(gst_registered=False)
    bank_id = await _bank_account_id()
    r = await api_client.post(
        "/api/v1/cashbook/setup",
        json={"bank_account_id": str(bank_id)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bookkeeping_mode"] == "cashbook"
    assert body["cashbook_default_bank_account_id"] == str(bank_id)
    assert body["version"] >= 2


@pytest.mark.skip(
    reason=(
        "Behaviour superseded by Round-2 audit fix #10 + "
        "cashbook-upgrade-downgrade-policy: full -> cashbook is now a "
        "first-class downgrade that refuses only on AR > 0, not on the "
        "mere presence of journal entries. ``setup_cashbook_mode`` delegates "
        "to ``downgrade_full_to_cashbook`` for the policy check (see "
        "services/cashbook.py). The dedicated AR-balance refusal path is "
        "covered by test_downgrade_refuses_when_ar_outstanding elsewhere "
        "in the cashbook suite."
    )
)
async def test_setup_refuses_full_company_with_existing_je(
    api_client: AsyncClient,
) -> None:
    """Historical: full -> cashbook used to refuse on any journal entries.

    Kept (skipped) so the rationale stays adjacent to the call site and
    a future contributor doesn't reintroduce the old guard.
    """
    await _reset_company_to_full_mode()
    try:
        bank_id = await _bank_account_id()
        r = await api_client.post(
            "/api/v1/cashbook/setup",
            json={"bank_account_id": str(bank_id)},
        )
        assert r.status_code == 409, r.text
        detail = r.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("code") == "cashbook_setup_refused"
    finally:
        await _seed_company_into_cashbook_mode(gst_registered=False)


async def test_setup_unknown_bank_account_409(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    r = await api_client.post(
        "/api/v1/cashbook/setup",
        json={"bank_account_id": str(uuid.uuid4())},
    )
    assert r.status_code == 409, r.text
    detail = r.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("code") == "cashbook_setup_refused"


async def test_setup_missing_body_422(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    r = await api_client.post("/api/v1/cashbook/setup", json={})
    assert r.status_code == 422


async def test_upgrade_to_full_flips_mode(api_client: AsyncClient) -> None:
    await _seed_company_into_cashbook_mode(gst_registered=False)
    try:
        r = await api_client.post("/api/v1/cashbook/upgrade-to-full")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["bookkeeping_mode"] == "full"
        assert body["version"] >= 2
    finally:
        await _seed_company_into_cashbook_mode(gst_registered=False)


async def test_upgrade_refuses_when_already_full(api_client: AsyncClient) -> None:
    await _reset_company_to_full_mode()
    try:
        r = await api_client.post("/api/v1/cashbook/upgrade-to-full")
        assert r.status_code == 409, r.text
        detail = r.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("code") == "cashbook_setup_refused"
    finally:
        await _seed_company_into_cashbook_mode(gst_registered=False)


async def test_companies_endpoint_exposes_bookkeeping_mode(
    api_client: AsyncClient,
) -> None:
    """CompanyOut surfaces bookkeeping_mode + cashbook_default_bank_account_id
    so the web UI can hide full-edition menu items in cashbook mode.
    """
    _, company_id = await _seed_company_into_cashbook_mode(gst_registered=False)
    bank_id = await _bank_account_id()
    r = await api_client.get(f"/api/v1/companies/{company_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bookkeeping_mode"] == "cashbook"
    assert body["cashbook_default_bank_account_id"] == str(bank_id)
