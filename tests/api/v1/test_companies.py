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
# POST /companies — EE fields (Packet 1a)
# ---------------------------------------------------------------------------


async def test_post_ee_company_persists_ee_fields(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST with jurisdiction=EE + registrikood/kmv_number persists and returns them."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    tag = uuid.uuid4().hex[:8]
    name = f"EECo_{tag}"
    try:
        resp = await api_client.post(
            "/api/v1/companies",
            json={
                "name": name,
                "jurisdiction": "EE",
                "coa_template_key": "ee/default",
                "registrikood": "12345678",
                "kmv_number": "EE123456789",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["jurisdiction"] == "EE"
        assert body["coa_template_key"] == "ee/default"
        assert body["registrikood"] == "12345678"
        assert body["kmv_number"] == "EE123456789"

        # Get round-trips the same fields.
        get_resp = await api_client.get(f"/api/v1/companies/{body['id']}")
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["jurisdiction"] == "EE"
        assert get_body["registrikood"] == "12345678"
        assert get_body["kmv_number"] == "EE123456789"
    finally:
        from sqlalchemy import delete

        from saebooks.models.company import Company

        async with AsyncSessionLocal() as session:
            await session.execute(delete(Company).where(Company.name == name))
            await session.commit()


async def test_patch_ee_company_registrikood_whitespace_stripped(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3, finding 4: an otherwise-valid registrikood/kmv_number with
    incidental leading/trailing whitespace is stripped before format
    validation, matching services.companies.update()'s own
    `.strip() or None` normalisation -- not rejected as malformed."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    tag = uuid.uuid4().hex[:8]
    name = f"EEWs_{tag}"
    try:
        create_resp = await api_client.post(
            "/api/v1/companies",
            json={
                "name": name,
                "jurisdiction": "EE",
                "coa_template_key": "ee/default",
                "registrikood": "12345678",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        body = create_resp.json()

        patch_resp = await api_client.patch(
            f"/api/v1/companies/{body['id']}",
            json={"registrikood": " 87654321", "kmv_number": "EE123456789 "},
            headers={"If-Match": str(body["version"])},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["registrikood"] == "87654321"
        assert patch_resp.json()["kmv_number"] == "EE123456789"
    finally:
        from sqlalchemy import delete

        from saebooks.models.company import Company

        async with AsyncSessionLocal() as session:
            await session.execute(delete(Company).where(Company.name == name))
            await session.commit()


async def test_post_au_company_unchanged_defaults(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST without jurisdiction still defaults to AU/au-default, EE fields NULL."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    tag = uuid.uuid4().hex[:8]
    name = f"AUCo_{tag}"
    try:
        resp = await api_client.post("/api/v1/companies", json={"name": name})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["jurisdiction"] == "AU"
        assert body["coa_template_key"] == "au/default"
        assert body["registrikood"] is None
        assert body["kmv_number"] is None
    finally:
        from sqlalchemy import delete

        from saebooks.models.company import Company

        async with AsyncSessionLocal() as session:
            await session.execute(delete(Company).where(Company.name == name))
            await session.commit()


async def test_patch_ee_company_clears_registrikood_and_kmv_number(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH with registrikood="" (whitespace) clears a previously-set
    value to NULL, same as every other optional string column — not a
    422 (critic round 1, finding 4)."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    tag = uuid.uuid4().hex[:8]
    name = f"EEClear_{tag}"
    try:
        create_resp = await api_client.post(
            "/api/v1/companies",
            json={
                "name": name,
                "jurisdiction": "EE",
                "coa_template_key": "ee/default",
                "registrikood": "12345678",
                "kmv_number": "EE123456789",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        body = create_resp.json()
        company_id, version = body["id"], body["version"]

        patch_resp = await api_client.patch(
            f"/api/v1/companies/{company_id}",
            json={"registrikood": "  ", "kmv_number": ""},
            headers={"If-Match": str(version)},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["registrikood"] is None
        assert patch_resp.json()["kmv_number"] is None

        get_resp = await api_client.get(f"/api/v1/companies/{company_id}")
        assert get_resp.json()["registrikood"] is None
        assert get_resp.json()["kmv_number"] is None
    finally:
        from sqlalchemy import delete

        from saebooks.models.company import Company

        async with AsyncSessionLocal() as session:
            await session.execute(delete(Company).where(Company.name == name))
            await session.commit()


async def test_patch_ee_company_clears_ar_control_account_resolves_to_ee_default(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixer round 4: PATCH clearing an EE company's ar_control_account_code
    override must not leave it silently resolving to the AU convention code
    ("1-1200"), which doesn't exist in an EE chart. The company's own
    ee/default chart carries the EE control accounts (1200/2100), so after
    clearing, get_ar_account (the exact posting-time lookup invoices/bills
    use) must resolve to a real Account row, not raise "control account
    missing"."""
    from saebooks.config import settings as app_settings
    from saebooks.services import control_accounts as control_accounts_svc

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    tag = uuid.uuid4().hex[:8]
    name = f"EEClearAR_{tag}"
    try:
        # CompanyCreate has no ar_control_account_code field -- chart_ee's
        # applier is what sets it (to "1200") at creation time.
        create_resp = await api_client.post(
            "/api/v1/companies",
            json={
                "name": name,
                "jurisdiction": "EE",
                "coa_template_key": "ee/default",
                "registrikood": "87654321",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        body = create_resp.json()
        company_id, version = body["id"], body["version"]
        assert body["ar_control_account_code"] == "1200"

        # PATCH an explicit override to a different real EE account first,
        # to prove this is a genuine "explicit override -> cleared" flow
        # rather than a no-op (both the default and chart_ee's initial
        # value happen to be "1200").
        override_resp = await api_client.patch(
            f"/api/v1/companies/{company_id}",
            json={"ar_control_account_code": "1300"},
            headers={"If-Match": str(version)},
        )
        assert override_resp.status_code == 200, override_resp.text
        version = override_resp.json()["version"]

        # Clear the override -- the guard must accept this (it does not
        # blend with ap, which is still unset -> its own EE default).
        patch_resp = await api_client.patch(
            f"/api/v1/companies/{company_id}",
            json={"ar_control_account_code": ""},
            headers={"If-Match": str(version)},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["ar_control_account_code"] is None

        async with AsyncSessionLocal() as session:
            resolved = await control_accounts_svc.resolve_ar_code(
                session, uuid.UUID(company_id)
            )
            assert resolved == "1200"

            # The exact posting-time lookup (invoices/bills route through
            # this) must resolve to a real Account row in this EE
            # company's chart, not raise "AR control account missing".
            account = await control_accounts_svc.get_ar_account(
                session, uuid.UUID(company_id)
            )
            assert account.code == "1200"
    finally:
        from sqlalchemy import delete

        from saebooks.models.company import Company

        async with AsyncSessionLocal() as session:
            await session.execute(delete(Company).where(Company.name == name))
            await session.commit()


async def test_post_ee_company_malformed_registrikood_422(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed registrikood (not 8 digits) on an EE company is rejected 422."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    resp = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"BadEE_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "EE",
            "registrikood": "not-digits",
        },
    )
    assert resp.status_code == 422


async def test_post_ee_company_malformed_kmv_number_422_field_scoped(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixer round 4: a malformed kmv_number on CompanyCreate must report a
    field-scoped loc (['body', 'kmv_number']), not a bare model-level
    loc=['body'] -- format checking now runs in a field_validator (mirrors
    CompanyUpdate.kmv_number_format) instead of inline in the cross-field
    model_validator, so callers like saebooks-web can place the error next
    to the right form input.

    The engine's RFC 7807 handler (saebooks/api/errors.py) fires whenever
    the caller's Accept header satisfies its _wants_json check -- true by
    default for httpx.AsyncClient (it always sends "Accept: */*" unless
    overridden, incl. this test's own ``api_client`` fixture) -- so the
    field-scoped pydantic error list lives under "errors", not "detail"
    ("detail" is a fixed human string in that shape)."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    resp = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"BadKMV_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "EE",
            "registrikood": f"{uuid.uuid4().int % 90000000 + 10000000}",
            "kmv_number": "BOGUS",
        },
    )
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert any(err["loc"] == ["body", "kmv_number"] for err in errors), errors


async def test_post_ee_company_missing_registrikood_422(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3, finding 1: registrikood is mandatory for jurisdiction=EE
    at the engine layer, not just the web UI -- omitting it entirely
    (key absent) must 422, not silently persist a company with
    registrikood=NULL."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    resp = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"NoRegNo_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "EE",
            "coa_template_key": "ee/default",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_post_company_unknown_jurisdiction_422(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecognised jurisdiction code is rejected 422."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    resp = await api_client.post(
        "/api/v1/companies",
        json={"name": f"ZZCo_{uuid.uuid4().hex[:8]}", "jurisdiction": "ZZ"},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("jurisdiction", ["NZ", "UK"])
async def test_post_company_stub_jurisdiction_422(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, jurisdiction: str
) -> None:
    """NZ/UK are registered engine slots (fail loudly, by name) but not
    ready jurisdictions to create a company against — 422, not a silent
    201 with an unimplemented jurisdiction persisted (critic round 1,
    finding 2)."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    resp = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"Stub_{uuid.uuid4().hex[:8]}",
            "jurisdiction": jurisdiction,
        },
    )
    assert resp.status_code == 422


async def test_post_company_mismatched_template_and_jurisdiction_422(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """jurisdiction=AU with an EE coa_template_key (or vice versa) is
    rejected rather than persisting a company whose chart language/tax
    codes don't match its stored jurisdiction (critic round 1, finding 3)."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    resp = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"Mismatch_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "AU",
            "coa_template_key": "ee/default",
        },
    )
    assert resp.status_code == 422

    resp2 = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"Mismatch2_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "EE",
            "registrikood": "12345678",
            # coa_template_key omitted -> defaults to "au/default", must
            # not silently create an EE company with zero accounts.
        },
    )
    assert resp2.status_code == 422


async def test_post_company_ee_fields_rejected_for_non_ee_jurisdiction(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """registrikood/kmv_number are EE-only at create time too — mirrors
    services.companies.update()'s existing guard (critic round 1,
    finding 5).

    Fixer round 4: uses a WELL-FORMED registrikood -- since registrikood_
    format is now a field_validator (runs unconditionally, before the
    cross-field model_validator that owns this guard), a malformed value
    would 422 via the format check instead, and this test would pass for
    the wrong reason without actually exercising the EE-only guard."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    resp = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"AUWithEE_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "AU",
            "registrikood": "12345678",
        },
    )
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert any("can only be set on an EE company" in err["msg"] for err in errors), errors


async def test_post_company_unregistered_template_key_422(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critic round 2, finding 1/2/4: a coa_template_key whose *prefix*
    matches jurisdiction but isn't an actually-registered template (a
    typo, e.g. "ee/defualt") must be rejected 422 at the schema layer —
    not accepted, then blow up 500 inside create_company after the
    Company row is already committed."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    resp = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"Typo_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "EE",
            "registrikood": "12345678",
            "coa_template_key": "ee/defualt",
        },
    )
    assert resp.status_code == 422

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Company).where(
                    Company.jurisdiction == "EE", Company.registrikood == "12345678"
                )
            )
        ).scalars().all()
        assert rows == []


async def test_post_company_neutral_jurisdiction_succeeds(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critic round 2, finding 1: jurisdiction="XX" (the neutral sentinel,
    zero jurisdiction modules) is advertised by known_jurisdictions() and
    must actually be able to create a company, not dead-end in an
    unhandled 500 from an unregistered "xx/default" template."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    resp = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"Neutral_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "XX",
            "coa_template_key": "xx/default",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["jurisdiction"] == "XX"
    assert body["coa_template_key"] == "xx/default"


async def test_post_company_neutral_jurisdiction_omitted_template_succeeds(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixer round 4: jurisdiction="XX" with coa_template_key OMITTED must
    also succeed -- the schema's own class default ("au/default") does not
    match "XX" and previously 422'd, contradicting the stated "company
    creation works with zero jurisdiction modules" design goal for the one
    jurisdiction it is supposed to hold for by construction."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    resp = await api_client.post(
        "/api/v1/companies",
        json={"name": f"NeutralNoKey_{uuid.uuid4().hex[:8]}", "jurisdiction": "XX"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["jurisdiction"] == "XX"
    assert body["coa_template_key"] == "xx/default"


async def test_post_company_duplicate_registrikood_409(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critic round 2, finding 3: two companies cannot claim the same
    registrikood for the same tenant — the second POST is rejected
    (unique constraint 0204), not silently persisted as a duplicate."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    registrikood = str(uuid.uuid4().int)[:8]
    first = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"DupA_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "EE",
            "registrikood": registrikood,
            "coa_template_key": "ee/default",
        },
    )
    assert first.status_code == 201

    second = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"DupB_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "EE",
            "registrikood": registrikood,
            "coa_template_key": "ee/default",
        },
    )
    assert second.status_code == 409

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Company).where(Company.registrikood == registrikood)
            )
        ).scalars().all()
        assert len(rows) == 1


async def test_patch_company_duplicate_registrikood_409(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3, finding 2: PATCHing company B's registrikood to a value
    already held by company A hits the same tenant-scoped unique
    constraint (0204) as POST -- must return a clean 409, not a bare
    500 from an uncaught IntegrityError."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    registrikood = str(uuid.uuid4().int)[:8]
    first = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"PatchDupA_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "EE",
            "registrikood": registrikood,
            "coa_template_key": "ee/default",
        },
    )
    assert first.status_code == 201, first.text

    second = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"PatchDupB_{uuid.uuid4().hex[:8]}",
            "jurisdiction": "EE",
            "registrikood": str(uuid.uuid4().int)[:8],
            "coa_template_key": "ee/default",
        },
    )
    assert second.status_code == 201, second.text
    body_b = second.json()

    patch_resp = await api_client.patch(
        f"/api/v1/companies/{body_b['id']}",
        json={"registrikood": registrikood},
        headers={"If-Match": str(body_b["version"])},
    )
    assert patch_resp.status_code == 409, patch_resp.text

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Company).where(Company.registrikood == registrikood)
            )
        ).scalars().all()
        assert len(rows) == 1


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
# HOBB-1 — tax_registered + gst_effective_date fields
# ---------------------------------------------------------------------------


async def test_companies_gst_fields_present_in_response(api_client: AsyncClient) -> None:
    """CompanyOut always includes tax_registered and gst_effective_date."""
    company_id, _ = await _get_seed_company()
    r = await api_client.get(f"/api/v1/companies/{company_id}")
    assert r.status_code == 200
    body = r.json()
    assert "tax_registered" in body
    assert isinstance(body["tax_registered"], bool)
    assert "gst_effective_date" in body


async def test_companies_patch_gst_fields(api_client: AsyncClient) -> None:
    """PATCH tax_registered + gst_effective_date round-trips correctly."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"tax_registered": True, "gst_effective_date": "2024-07-01"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tax_registered"] is True
    assert body["gst_effective_date"] == "2024-07-01"
    assert body["version"] == version + 1

    # Restore
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"tax_registered": False},
        headers={"If-Match": str(version + 1)},
    )


async def test_companies_patch_asset_disposal_account_override(api_client: AsyncClient) -> None:
    """PATCH asset_disposal_gain/loss_account_code round-trips (M1.5 P1 tail)."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "asset_disposal_gain_account_code": "4-6000",
            "asset_disposal_loss_account_code": "6-2050",
        },
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["asset_disposal_gain_account_code"] == "4-6000"
    assert body["asset_disposal_loss_account_code"] == "6-2050"
    assert body["version"] == version + 1

    # Empty string clears back to NULL; restores state for other tests.
    r2 = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "asset_disposal_gain_account_code": "",
            "asset_disposal_loss_account_code": "",
        },
        headers={"If-Match": str(version + 1)},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["asset_disposal_gain_account_code"] is None
    assert body2["asset_disposal_loss_account_code"] is None


async def test_companies_patch_lifecycle_status(api_client: AsyncClient) -> None:
    """PATCH lifecycle_status round-trips + rejects unknown values (M1.5 P1 tail)."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"lifecycle_status": "dormant"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifecycle_status"] == "dormant"
    assert body["version"] == version + 1

    bad = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"lifecycle_status": "not_a_status"},
        headers={"If-Match": str(version + 1)},
    )
    assert bad.status_code == 422, bad.text

    # Restore
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"lifecycle_status": "active"},
        headers={"If-Match": str(version + 1)},
    )


async def test_companies_patch_industry_code(api_client: AsyncClient) -> None:
    """PATCH industry_code round-trips (M1.5 P1 tail)."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"industry_code": "6920"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["industry_code"] == "6920"
    assert body["version"] == version + 1

    # Empty string clears back to NULL; restores state for other tests.
    r2 = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"industry_code": ""},
        headers={"If-Match": str(version + 1)},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["industry_code"] is None


async def test_companies_patch_fin_year_start_day(api_client: AsyncClient) -> None:
    """PATCH fin_year_start_day round-trips + validates 1-31 (M1.5 P1 tail)."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"fin_year_start_day": 6},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fin_year_start_day"] == 6
    assert body["version"] == version + 1

    bad = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"fin_year_start_day": 32},
        headers={"If-Match": str(version + 1)},
    )
    assert bad.status_code == 422, bad.text

    # Restore
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"fin_year_start_day": 1},
        headers={"If-Match": str(version + 1)},
    )


async def test_companies_patch_fin_year_start_day_invalid_for_month_422(
    api_client: AsyncClient,
) -> None:
    """PATCH cross-validates day against month when BOTH arrive together
    (period-picker engine spec 2026-07-21): 31 is invalid for a 30-day
    month, and Feb 29/30/31 is rejected outright (leap-year ambiguity),
    not silently clamped."""
    company_id, version = await _get_seed_company()

    # month=4 (April, 30 days) + day=31 together -> 422.
    bad_30day = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"fin_year_start_month": 4, "fin_year_start_day": 31},
        headers={"If-Match": str(version)},
    )
    assert bad_30day.status_code == 422, bad_30day.text

    # month=2 (February) + day=29 together -> 422, even though 2026/2028
    # etc. are leap years -- the field has no year, so this is rejected
    # outright rather than clamped.
    bad_feb29 = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"fin_year_start_month": 2, "fin_year_start_day": 29},
        headers={"If-Match": str(version)},
    )
    assert bad_feb29.status_code == 422, bad_feb29.text

    # month=2 + day=28 together -> valid.
    ok_feb28 = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"fin_year_start_month": 2, "fin_year_start_day": 28},
        headers={"If-Match": str(version)},
    )
    assert ok_feb28.status_code == 200, ok_feb28.text
    body = ok_feb28.json()
    assert body["fin_year_start_month"] == 2
    assert body["fin_year_start_day"] == 28
    new_version = body["version"]

    # Belt-and-braces: a LONE day=31 PATCH against the now-February company
    # (service-layer cross-check against the final resolved state, since
    # the schema layer alone can't see the company's stored month).
    bad_lone_day = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"fin_year_start_day": 31},
        headers={"If-Match": str(new_version)},
    )
    assert bad_lone_day.status_code == 422, bad_lone_day.text

    # Restore to the original month=7/day=1 seed state.
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"fin_year_start_month": 7, "fin_year_start_day": 1},
        headers={"If-Match": str(new_version)},
    )


async def test_post_company_fin_year_start_day_defaults_and_roundtrips(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST accepts fin_year_start_day, defaulting to 1 when omitted and
    round-tripping through GET when supplied explicitly."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    tag = uuid.uuid4().hex[:8]
    default_name = f"FYDefault_{tag}"
    explicit_name = f"FYExplicit_{tag}"
    try:
        default_resp = await api_client.post(
            "/api/v1/companies", json={"name": default_name}
        )
        assert default_resp.status_code == 201, default_resp.text
        assert default_resp.json()["fin_year_start_day"] == 1

        explicit_resp = await api_client.post(
            "/api/v1/companies",
            json={
                "name": explicit_name,
                "fin_year_start_month": 4,
                "fin_year_start_day": 15,
            },
        )
        assert explicit_resp.status_code == 201, explicit_resp.text
        body = explicit_resp.json()
        assert body["fin_year_start_month"] == 4
        assert body["fin_year_start_day"] == 15

        get_resp = await api_client.get(f"/api/v1/companies/{body['id']}")
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["fin_year_start_day"] == 15
    finally:
        from sqlalchemy import delete

        from saebooks.models.company import Company

        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(Company).where(
                    Company.name.in_([default_name, explicit_name])
                )
            )
            await session.commit()


async def test_post_company_fin_year_start_day_invalid_for_month_422(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST rejects a day that can never occur in the chosen start month
    at the schema layer (422 before a Company row is ever created)."""
    from saebooks.config import settings as app_settings

    monkeypatch.setattr(app_settings, "edition", "enterprise")

    tag = uuid.uuid4().hex[:8]

    # 30-day month (September) + day=31.
    resp_30day = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"FYBad30_{tag}",
            "fin_year_start_month": 9,
            "fin_year_start_day": 31,
        },
    )
    assert resp_30day.status_code == 422, resp_30day.text

    # February + day=29 (leap-year-only, rejected outright not clamped).
    resp_feb29 = await api_client.post(
        "/api/v1/companies",
        json={
            "name": f"FYBadFeb_{tag}",
            "fin_year_start_month": 2,
            "fin_year_start_day": 29,
        },
    )
    assert resp_feb29.status_code == 422, resp_feb29.text

    # Neither invalid request should have left a row behind.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Company).where(Company.name.like(f"FYBad%{tag}"))
        )
        assert result.scalars().first() is None


async def test_companies_patch_letterhead_and_terms_fields(api_client: AsyncClient) -> None:
    """PATCH phone/email/website/default_payment_terms round-trips (0171)."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "phone": "07 4000 0000",
            "email": "accounts@example.com",
            "website": "https://saebooks.com.au",
            "default_payment_terms": "Payment within 14 days of invoice date.",
        },
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phone"] == "07 4000 0000"
    assert body["email"] == "accounts@example.com"
    assert body["website"] == "https://saebooks.com.au"
    assert body["default_payment_terms"] == "Payment within 14 days of invoice date."
    assert body["version"] == version + 1

    # Empty string clears back to NULL; restores state for other tests.
    r2 = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"phone": "", "email": "", "website": "", "default_payment_terms": ""},
        headers={"If-Match": str(version + 1)},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["phone"] is None
    assert body2["email"] is None
    assert body2["website"] is None
    assert body2["default_payment_terms"] is None


async def test_companies_patch_remittance_fields(api_client: AsyncClient) -> None:
    """The 0168 remittance columns are exposed + writable via the API (0171)."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "bank_name": "Westpac",
            "bank_bsb": "034-193",
            "bank_account_number": "485846",
            "bank_account_name": "Example Pty Ltd",
            "payment_terms_text": "Late payments accrue 2.5%/month.",
            "terms_url": "https://saebooks.com.au/terms",
        },
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bank_name"] == "Westpac"
    assert body["bank_bsb"] == "034-193"
    assert body["bank_account_number"] == "485846"
    assert body["bank_account_name"] == "Example Pty Ltd"
    assert body["payment_terms_text"] == "Late payments accrue 2.5%/month."
    assert body["terms_url"] == "https://saebooks.com.au/terms"

    # Restore (clear) so PDF-related tests see pre-test state.
    r2 = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={
            "bank_name": "",
            "bank_bsb": "",
            "bank_account_number": "",
            "bank_account_name": "",
            "payment_terms_text": "",
            "terms_url": "",
        },
        headers={"If-Match": str(version + 1)},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["bank_account_number"] is None


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
# Bidirectional cashbook <-> full per cashbook-upgrade-downgrade-policy.
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


# ---------------------------------------------------------------------------
# AR/AP control-account override (0198, Packet 4b) — same "plain company
# column, round-trips via PATCH" pattern as bad_debt_recovery_account above.
# ---------------------------------------------------------------------------


async def test_companies_control_account_defaults(api_client: AsyncClient) -> None:
    """NULL by default — engine resolves the AU convention codes."""
    company_id, _ = await _get_seed_company()
    r = await api_client.get(f"/api/v1/companies/{company_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ar_control_account_code"] is None
    assert body["ap_control_account_code"] is None


async def test_companies_patch_control_accounts_round_trip(
    api_client: AsyncClient,
) -> None:
    """PATCH the AR/AP control-account override; persists and bumps version."""
    company_id, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"ar_control_account_code": "1000", "ap_control_account_code": "2000"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ar_control_account_code"] == "1000"
    assert body["ap_control_account_code"] == "2000"
    assert body["version"] == version + 1

    # Restore defaults.
    await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"ar_control_account_code": None, "ap_control_account_code": None},
        headers={"If-Match": str(version + 1)},
    )


async def _reset_control_accounts_to_null(api_client: AsyncClient, company_id: str) -> int:
    """Force both control-account columns to NULL and return the resulting
    version. Self-contained setup so the collision tests below don't
    depend on some earlier test's cleanup having actually landed (the
    round-trip test above never asserts its own restore PATCH -- and
    that restore is itself a no-op: JSON ``null`` is indistinguishable
    from "field omitted" once ``exclude_unset=True``/the ``is not
    None`` guards collapse it, so clearing a column here means PATCHing
    an empty string, per the ``address``-block comment a few lines
    above the guard in ``companies.update``)."""
    _, version = await _get_seed_company()
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"ar_control_account_code": "", "ap_control_account_code": ""},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ar_control_account_code"] is None
    assert body["ap_control_account_code"] is None
    return body["version"]


async def test_companies_patch_control_accounts_explicit_collision_rejected(
    api_client: AsyncClient,
) -> None:
    """Both sides explicitly set to the same code is rejected."""
    company_id, _ = await _get_seed_company()
    version = await _reset_control_accounts_to_null(api_client, company_id)
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"ar_control_account_code": "9000", "ap_control_account_code": "9000"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 422, r.text


async def test_companies_patch_control_accounts_ap_collides_with_ar_default(
    api_client: AsyncClient,
) -> None:
    """Critic round 3: ar_control_account_code is left NULL (resolves to the
    AR default "1-1200") while ap_control_account_code is explicitly PATCHed
    to that same code -- must be rejected even though the two *stored*
    columns never literally match."""
    company_id, _ = await _get_seed_company()
    version = await _reset_control_accounts_to_null(api_client, company_id)
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"ap_control_account_code": "1-1200"},
        headers={"If-Match": str(version)},
    )
    assert r.status_code == 422, r.text


async def test_companies_patch_control_accounts_ar_collides_with_ap_default(
    api_client: AsyncClient,
) -> None:
    """Symmetric case: ap_control_account_code left NULL (resolves to the
    AP default "2-1200") while ar_control_account_code is explicitly
    PATCHed to that same code."""
    company_id, _ = await _get_seed_company()
    version = await _reset_control_accounts_to_null(api_client, company_id)
    r = await api_client.patch(
        f"/api/v1/companies/{company_id}",
        json={"ar_control_account_code": "2-1200"},
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
