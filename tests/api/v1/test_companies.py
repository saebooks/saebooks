"""Phase 1 contract tests for /api/v1/companies.

Covers:
* Auth gate (401 without bearer)
* List — returns active companies with version field
* Get — 200 (existing seed company), 404 for unknown UUID
* Update — PATCH with correct If-Match bumps version + appends change_log row
* Update — stale If-Match → 409
* Update — missing If-Match → 428
* change_log row appended on update
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from saebooks.api.v1.auth import current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.change_log import ChangeLog
from saebooks.models.company import Company

pytestmark = pytest.mark.postgres_only


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


async def _get_seed_company() -> tuple[str, int]:
    """Return (id, version) of the first active company in the test DB."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
        )
        company = result.scalars().first()
        if company is None:
            raise RuntimeError("No seed company in test DB — run alembic upgrade head first")
        return str(company.id), company.version


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


async def test_companies_requires_bearer(unauth_client: AsyncClient) -> None:
    r = await unauth_client.get("/api/v1/companies")
    assert r.status_code == 401


async def test_companies_rejects_wrong_token(unauth_client: AsyncClient) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer totally-wrong"},
    ) as ac:
        r = await ac.get("/api/v1/companies")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_companies_list_200(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/v1/companies")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert body["total"] >= 1
    # Every item must have a version field (Phase 1 requirement)
    for item in body["items"]:
        assert "version" in item
        assert isinstance(item["version"], int)
        assert item["version"] >= 1


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


async def test_companies_get_200(api_client: AsyncClient) -> None:
    company_id, _ = await _get_seed_company()
    r = await api_client.get(f"/api/v1/companies/{company_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == company_id
    assert "version" in body


async def test_companies_get_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/companies/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Update — valid If-Match
# ---------------------------------------------------------------------------


async def test_companies_update_bumps_version(api_client: AsyncClient) -> None:
    company_id, version = await _get_seed_company()
    new_name = f"Updated Co {uuid.uuid4().hex[:6]}"
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": new_name},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == version + 1
    assert body["trading_name"] == new_name

    # Restore trading name for subsequent tests
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": "SAE Engineering"},
        headers={"If-Match": str(version + 1)},
    )


# ---------------------------------------------------------------------------
# Update — missing If-Match → 428
# ---------------------------------------------------------------------------


async def test_companies_update_requires_if_match(api_client: AsyncClient) -> None:
    company_id, _ = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": "should fail"},
    )
    assert r.status_code == 428


# ---------------------------------------------------------------------------
# Update — stale If-Match → 409
# ---------------------------------------------------------------------------


async def test_companies_stale_if_match_409(api_client: AsyncClient) -> None:
    company_id, _ = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": "stale"},
        headers={"If-Match": "9999"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == "version mismatch"
    assert body["current"]["id"] == company_id


# ---------------------------------------------------------------------------
# change_log row appended on update
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /companies — gated on FLAG_MULTI_COMPANY
# ---------------------------------------------------------------------------


async def test_post_company_requires_feature_flag(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /companies returns 404 when FLAG_MULTI_COMPANY is disabled (community edition)."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "community")
    resp = await api_client.post("/api/v1/companies", json={"name": "TestCo"})
    assert resp.status_code == 404


async def test_post_company_creates_with_enterprise_edition(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /companies creates a company when FLAG_MULTI_COMPANY is enabled (enterprise = unlimited cap)."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    tag = uuid.uuid4().hex[:8]
    name = f"TestCo_{tag}"
    try:
        resp = await api_client.post("/api/v1/companies", json={"name": name})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == name
        assert "id" in body
        assert body["version"] == 1
    finally:
        # Cleanup the row so company-cap counters in neighbouring tests stay sane.
        from sqlalchemy import delete

        from saebooks.models.company import Company

        async with AsyncSessionLocal() as session:
            await session.execute(delete(Company).where(Company.name == name))
            await session.commit()


# ---------------------------------------------------------------------------
# X-Company-Id header — get_active_company_id dep
# ---------------------------------------------------------------------------


async def test_x_company_id_header_invalid_uuid_returns_400(api_client: AsyncClient) -> None:
    """X-Company-Id with a malformed UUID returns 400 from get_active_company_id."""
    resp = await api_client.get(
        "/api/v1/contacts",
        headers={"X-Company-Id": "not-a-uuid"},
    )
    assert resp.status_code == 400


async def test_x_company_id_header_unknown_uuid_returns_404(api_client: AsyncClient) -> None:
    """X-Company-Id with a UUID that does not belong to the tenant returns 404."""
    resp = await api_client.get(
        "/api/v1/contacts",
        headers={"X-Company-Id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


async def test_x_company_id_header_valid_uuid_returns_200(api_client: AsyncClient) -> None:
    """X-Company-Id with a tenant-owned UUID resolves and returns 200."""
    company_id, _ = await _get_seed_company()
    resp = await api_client.get(
        "/api/v1/contacts",
        headers={"X-Company-Id": company_id},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# HOBB-1 — gst_registered + gst_effective_date fields
# ---------------------------------------------------------------------------


async def test_companies_gst_fields_present_in_response(api_client: AsyncClient) -> None:
    """CompanyOut always includes gst_registered and gst_effective_date."""
    company_id, _ = await _get_seed_company()
    r = await api_client.get(f"/api/v1/companies/{company_id}")
    assert r.status_code == 200
    body = r.json()
    assert "gst_registered" in body
    assert isinstance(body["gst_registered"], bool)
    assert "gst_effective_date" in body


async def test_companies_patch_gst_fields(api_client: AsyncClient) -> None:
    """PATCH gst_registered + gst_effective_date round-trips correctly."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"gst_registered": True, "gst_effective_date": "2024-07-01"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gst_registered"] is True
    assert body["gst_effective_date"] == "2024-07-01"
    assert body["version"] == version + 1

    # Restore
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"gst_registered": False},
        headers={"If-Match": str(version + 1)},
    )


async def test_companies_gst_effective_date_future_rejected(api_client: AsyncClient) -> None:
    """gst_effective_date in the future returns 422."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"gst_effective_date": "2099-01-01"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 422


async def test_companies_change_log_on_update(api_client: AsyncClient) -> None:
    company_id, version = await _get_seed_company()

    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(select(ChangeLog.id).order_by(ChangeLog.id.desc()).limit(1))
        ).scalar_one_or_none() or 0

    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": f"LogTest {uuid.uuid4().hex[:6]}"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200

    new_version = r.json()["version"]

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.id > before,
                    ChangeLog.entity_id == uuid.UUID(company_id),
                    ChangeLog.entity == "company",
                )
                .order_by(ChangeLog.id)
            )
        ).scalars().all()

    assert len(rows) >= 1
    assert rows[-1].op == "update"
    assert rows[-1].version == new_version

    # Restore version for next test
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"trading_name": "SAE Engineering"},
        headers={"If-Match": str(new_version)},
    )


# ---------------------------------------------------------------------------
# HOBB-5 — GST backdating: 4-year limit + backdate-preview endpoint
# ---------------------------------------------------------------------------


async def test_gst_effective_date_too_far_past_rejected(api_client: AsyncClient) -> None:
    """gst_effective_date more than 4 years in the past returns 422."""
    company_id, version = await _get_seed_company()
    from datetime import date

    five_years_ago = (date.today().replace(year=date.today().year - 5)).isoformat()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"gst_effective_date": five_years_ago},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 422


async def test_gst_backdate_preview_200(api_client: AsyncClient) -> None:
    """GET /gst-backdate-preview returns 200 with invoice_count."""
    company_id, _ = await _get_seed_company()
    from datetime import date

    one_year_ago = date.today().replace(year=date.today().year - 1).isoformat()
    r = await api_client.get(
        f"/api/v1/companies/{company_id}/gst-backdate-preview",
        params={"effective_date": one_year_ago},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "invoice_count" in body
    assert isinstance(body["invoice_count"], int)
    assert body["effective_date"] == one_year_ago


async def test_gst_backdate_preview_future_date_rejected(api_client: AsyncClient) -> None:
    """GET /gst-backdate-preview with a future date returns 422."""
    company_id, _ = await _get_seed_company()
    r = await api_client.get(
        f"/api/v1/companies/{company_id}/gst-backdate-preview",
        params={"effective_date": "2099-01-01"},
    )
    assert r.status_code == 422


async def test_gst_backdate_preview_too_far_past_rejected(api_client: AsyncClient) -> None:
    """GET /gst-backdate-preview with date > 4 years ago returns 422."""
    company_id, _ = await _get_seed_company()
    from datetime import date

    five_years_ago = date.today().replace(year=date.today().year - 5).isoformat()
    r = await api_client.get(
        f"/api/v1/companies/{company_id}/gst-backdate-preview",
        params={"effective_date": five_years_ago},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Round-2 audit fix #10: POST /companies/{id}/bookkeeping-mode
# Bidirectional cashbook <-> full per [[cashbook-upgrade-downgrade-policy]].
# ---------------------------------------------------------------------------


async def test_set_bookkeeping_mode_idempotent_when_already_target(
    api_client: AsyncClient,
) -> None:
    """current == target → 200 + current state unchanged."""
    company_id, _version = await _get_seed_company()
    # Seed company starts in 'full'. Pinging 'full' is a no-op.
    r = await api_client.post(
        f"/api/v1/companies/{company_id}/bookkeeping-mode",
        json={"mode": "full"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bookkeeping_mode"] == "full"
    assert str(body["company_id"]) == company_id


async def test_set_bookkeeping_mode_downgrade_refused_with_ar(
    api_client: AsyncClient,
) -> None:
    """A full-mode company with open AR cannot downgrade — error
    must list offending invoices."""
    from datetime import date as _date
    from decimal import Decimal as _Dec

    from sqlalchemy import select

    from saebooks.models.account import Account, AccountType
    from saebooks.models.contact import Contact, ContactType
    from saebooks.models.tax_code import TaxCode
    from saebooks.services import invoices as inv_svc

    company_id, _version = await _get_seed_company()

    # Create a posted invoice with amount_paid = 0 → open AR balance.
    async with AsyncSessionLocal() as session:
        income = (
            await session.execute(
                select(Account).where(
                    Account.company_id == uuid.UUID(company_id),
                    Account.account_type == AccountType.INCOME,
                    Account.is_header.is_(False),
                )
            )
        ).scalars().first()
        gst = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.company_id == uuid.UUID(company_id),
                    TaxCode.code == "GST",
                )
            )
        ).scalar_one()
        existing = (
            await session.execute(
                select(Contact).where(
                    Contact.company_id == uuid.UUID(company_id),
                    Contact.name == "Test Downgrade Co",
                )
            )
        ).scalars().first()
        if existing is None:
            contact = Contact(
                company_id=uuid.UUID(company_id),
                name="Test Downgrade Co",
                contact_type=ContactType.CUSTOMER,
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
        else:
            contact = existing

        bank = (
            await session.execute(
                select(Account).where(
                    Account.company_id == uuid.UUID(company_id),
                    Account.code == "1-1110",
                )
            )
        ).scalar_one()

        inv = await inv_svc.create_draft(
            session,
            company_id=uuid.UUID(company_id),
            contact_id=contact.id,
            issue_date=_date(2026, 6, 1),
            due_date=_date(2026, 6, 30),
            lines=[
                {
                    "description": "Open AR test",
                    "account_id": income.id,
                    "tax_code_id": gst.id,
                    "quantity": _Dec("1"),
                    "unit_price": _Dec("200.00"),
                    "discount_pct": _Dec("0"),
                }
            ],
        )
    async with AsyncSessionLocal() as session:
        await inv_svc.post_invoice(session, inv.id, posted_by="test")

    r = await api_client.post(
        f"/api/v1/companies/{company_id}/bookkeeping-mode",
        json={"mode": "cashbook", "bank_account_id": str(bank.id)},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    detail = body.get("detail") or body.get("message") or str(body)
    assert "open AR" in detail or "outstanding" in detail, (
        f"Expected an AR-balance error, got: {detail}"
    )


async def test_set_bookkeeping_mode_rejects_unknown_mode(
    api_client: AsyncClient,
) -> None:
    """POST with mode='banana' is rejected by pydantic pattern."""
    company_id, _ = await _get_seed_company()
    r = await api_client.post(
        f"/api/v1/companies/{company_id}/bookkeeping-mode",
        json={"mode": "banana"},
    )
    assert r.status_code in (422, 400), r.text


# ---------------------------------------------------------------------------
# Bad-debt company settings (Phase 2 / Task 7) — writeoff_mode,
# writeoff_threshold_days, recovery_mode, bad_debt_recovery_account.
# These persist as plain company columns and round-trip via PATCH, mirroring
# the psi_status / gst_* pattern. The web app reads/writes them through the
# existing /settings/company form.
# ---------------------------------------------------------------------------


async def test_companies_bad_debt_settings_defaults(api_client: AsyncClient) -> None:
    """A company exposes bad-debt settings with the documented defaults."""
    company_id, _ = await _get_seed_company()
    r = await api_client.get(f"/api/v1/companies/{company_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    # Defaults: review / 90 / smart_prompt / no explicit recovery account.
    assert body["writeoff_mode"] == "review"
    assert body["writeoff_threshold_days"] == 90
    assert body["recovery_mode"] == "smart_prompt"
    assert body["bad_debt_recovery_account"] is None


async def test_companies_patch_bad_debt_settings_round_trip(
    api_client: AsyncClient,
) -> None:
    """PATCH the four bad-debt settings; they persist and bump version."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "writeoff_mode": "auto",
            "writeoff_threshold_days": 120,
            "recovery_mode": "manual",
            "bad_debt_recovery_account": "4-1290",
        },
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["writeoff_mode"] == "auto"
    assert body["writeoff_threshold_days"] == 120
    assert body["recovery_mode"] == "manual"
    assert body["bad_debt_recovery_account"] == "4-1290"
    assert body["version"] == version + 1

    # Restore defaults.
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "writeoff_mode": "review",
            "writeoff_threshold_days": 90,
            "recovery_mode": "smart_prompt",
            "bad_debt_recovery_account": None,
        },
        headers={"If-Match": str(version + 1)},
    )


async def test_companies_patch_bad_debt_invalid_mode_rejected(
    api_client: AsyncClient,
) -> None:
    """An out-of-range writeoff_mode is rejected with 422."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"writeoff_mode": "banana"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 422, r.text


async def test_companies_patch_bad_debt_invalid_recovery_mode_rejected(
    api_client: AsyncClient,
) -> None:
    """An out-of-range recovery_mode is rejected with 422."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"recovery_mode": "banana"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 422, r.text


async def test_companies_patch_bad_debt_threshold_must_be_positive(
    api_client: AsyncClient,
) -> None:
    """writeoff_threshold_days <= 0 is rejected with 422."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"writeoff_threshold_days": 0},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 422, r.text
