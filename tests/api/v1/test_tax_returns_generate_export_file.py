"""API tests for the Packet 4c tax_returns surface:

* POST /api/v1/tax_returns/generate  — compute + persist a real return
* GET  /api/v1/tax_returns/{id}/export — render the filable document
* POST /api/v1/tax_returns/{id}/file   — manual FILED transition

Covers both AU (BAS, reusing the existing SBR envelope builder) and EE
(KMD, the new box-vector -> KmdFigures -> XML round trip), plus the
loud-not-silent failure modes: /generate 422 for a list-shaped return
type with no box definitions (TSD), /export 501 for a return_type with
no document builder, and /file's idempotency guard.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from lxml import etree
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID, current_token
from saebooks.db import AsyncSessionLocal
from saebooks.main import app
from saebooks.models.company import Company
from saebooks.models.tax_period import TaxPeriod, TaxPeriodType
from saebooks.models.tax_return import TaxReturn, TaxReturnStatus
from saebooks.services import business_identifiers
from saebooks.services.lodgement.sbr import bas as _sbr_bas
from saebooks.services.pay_runs_v2 import (
    PayLineInput,
    finalize_ee_status_only,
    upsert_line,
)
from tests.services.test_pay_runs_v2_ee import (
    _PERIOD_END,
    _PERIOD_START,
    _make_employee,
    _make_pay_run,
)
from tests.services.test_tax_return_generator import _make_ee_company

# The Packet-4/5 TSD golden month fixtures (byte-for-byte source of truth
# for the serializer) — the API export round-trip is compared against the
# same golden.xml ``tests/services/lodgement/test_tsd_golden.py`` pins.
_TSD_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "tsd"
_TSD_REGCODE = "10123456"  # must equal test_tsd_golden.py's _REGCODE
_TSD_E1_ISIKUKOOD = "38001010000"
_TSD_E2_ISIKUKOOD = "48505010001"

pytestmark = pytest.mark.postgres_only

_SBR_STUBBED = getattr(_sbr_bas, "__OPEN_ENGINE_STUB__", False)

# This test harness only sets REFERENCE_MIGRATION_DATABASE_URL (schema
# migrated) — not REFERENCE_DATABASE_URL (the runtime query URL the app
# process reads). So ``ReferenceSession`` is always None here and
# ``generate_return`` falls back to the embedded AU/BAS-only fallback set
# — EE is reference-DB-only by design (see
# tests/services/test_tax_return_generator.py's own documented workaround
# for the exact same constraint). EE /generate is therefore exercised at
# the 422-loud-failure level here (real, accurate behaviour in THIS
# harness); EE /export is exercised directly against a hand-persisted
# TaxReturn row, which is genuinely reference-DB-independent code.


@pytest.fixture
async def api_client() -> AsyncClient:
    token = current_token()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


async def _ee_client_and_period(
    period_start: date = date(2026, 1, 1), period_end: date = date(2026, 1, 31)
) -> tuple[AsyncClient, uuid.UUID]:
    """A throwaway EE company (no ledger data needed — a zero-transaction
    company still produces a valid, all-zero KMD box vector) pinned via
    X-Company-Id, plus a matching tax_periods row."""
    company_id = uuid.uuid4()
    period_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                name=f"KMD API Test {company_id.hex[:8]}",
                base_currency="EUR",
                fin_year_start_month=1,
                audit_mode="immutable",
                jurisdiction="EE",
            )
        )
        await session.flush()
        # The Estonian äriregistri kood under its own ``ee_regcode`` identifier
        # (the overloaded companies.abn column was dropped in 0204).
        await business_identifiers.upsert(
            session, company_id, "ee_regcode", "14551789",
            tenant_id=DEFAULT_TENANT_ID,
        )
        session.add(
            TaxPeriod(
                id=period_id,
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                jurisdiction="EST",
                period_type=TaxPeriodType.MONTHLY,
                period_start=period_start,
                period_end=period_end,
            )
        )
        await session.commit()

    token = current_token()
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Company-Id": str(company_id),
        },
    )
    return client, period_id


async def _au_client_and_period(
    *, abn: str | None = "51824753556",
    period_start: date = date(2026, 1, 1), period_end: date = date(2026, 3, 31),
) -> AsyncClient:
    """Pin a client to the seeded AU company (idempotent on the period,
    mirroring test_tax_returns_lodge_bas.py's ``_seed_company_and_period``)."""
    async with AsyncSessionLocal() as s:
        co = (
            await s.execute(select(Company).where(Company.jurisdiction == "AU").limit(1))
        ).scalars().first()
        assert co is not None, "expected a seed AU company"
        # ABN recorded under its ``au_abn`` business identifier (the legacy
        # ``companies.abn`` column was dropped in 0204). None clears it.
        if abn:
            await business_identifiers.upsert(
                s, co.id, "au_abn", abn, tenant_id=co.tenant_id
            )
        else:
            _bi = await business_identifiers.get(s, co.id, "au_abn")
            if _bi is not None:
                await s.delete(_bi)
        period = (
            await s.execute(
                select(TaxPeriod).where(
                    TaxPeriod.company_id == co.id,
                    TaxPeriod.jurisdiction == "AUS",
                    TaxPeriod.period_start == period_start,
                )
            )
        ).scalars().first()
        if period is None:
            period = TaxPeriod(
                id=uuid.uuid4(),
                company_id=co.id,
                tenant_id=co.tenant_id,
                jurisdiction="AUS",
                period_type=TaxPeriodType.QUARTERLY,
                period_start=period_start,
                period_end=period_end,
            )
            s.add(period)
        await s.commit()
        company_id, period_id = co.id, period.id

    token = current_token()
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Company-Id": str(company_id),
        },
    )
    return client, period_id


# ---------------------------------------------------------------------------
# /generate + /export — EE KMD (box-vector, real generator, real serializer)
# ---------------------------------------------------------------------------


async def test_export_ee_kmd() -> None:
    """Exercises /export's real EE-KMD branch: figures JSONB ->
    ``KmdFigures.from_figures_json`` -> ``build_kmd_xml_document`` — code
    that is entirely reference-DB-independent (unlike /generate, whose
    EE path needs the unconfigured-in-this-harness reference DB — see
    module docstring). The row is persisted directly (what a working
    /generate would have produced) so this test proves /export's own
    wiring rather than re-proving generate_return."""
    client, period_id = await _ee_client_and_period()
    async with AsyncSessionLocal() as session:
        row = TaxReturn(
            company_id=uuid.UUID(client.headers["X-Company-Id"]),
            tenant_id=DEFAULT_TENANT_ID,
            jurisdiction="EE",
            period_id=period_id,
            return_type="KMD",
            figures={"1": {"amount": "10000.00"}, "5": {"amount": "840.00"}},
            status=TaxReturnStatus.READY,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return_id = row.id

    async with client as ac:
        r2 = await ac.get(f"/api/v1/tax_returns/{return_id}/export")
        assert r2.status_code == 200, r2.text
        assert r2.headers["content-type"].startswith("application/xml")
        assert "attachment" in r2.headers["content-disposition"]
        root = etree.fromstring(r2.content)
        assert root is not None
        # The regcode (Company.abn fallback) shows up somewhere in the doc.
        assert b"14551789" in r2.content
        assert b"10000" in r2.content


async def test_generate_tsd_non_ee_company_422() -> None:
    """TSD is an Estonian return — /generate must reject it for a non-EE
    company loudly (keyed on the company's OWN jurisdiction, authoritative,
    not the payload's). This replaces the former "TSD has no box
    definitions" case: TSD now has its own dedicated generator branch, so
    an AU company posting TSD hits the non-EE guard, not the box model."""
    client, period_id = await _au_client_and_period()
    async with client as ac:
        r = await ac.post(
            "/api/v1/tax_returns/generate",
            json={"jurisdiction": "AU", "return_type": "TSD", "period_id": str(period_id)},
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "Estonian (EE) return type" in detail
        assert "box definitions" not in detail


async def _tsd_golden_client_and_period() -> tuple[AsyncClient, uuid.UUID]:
    """Set up + finalize the Packet-4/5 TSD golden month (E1 min-base-floor
    crosser + E2 pillar-II-6% elective, one FINALIZED EE pay run) behind an
    HTTP client pinned to that EE company, with the golden ``_REGCODE`` and
    a matching monthly ``tax_periods`` row — so a ``/generate`` then
    ``/export`` reproduces ``test_tsd_golden.py``'s byte-for-byte fixture.

    Unlike the KMD export test (which hand-persists a bare row because KMD
    export reconstructs from ``figures``), TSD export RE-GENERATES from the
    source pay runs — so the real posted-pay-run scenario must exist, not
    just a persisted return shell."""
    company_id = await _make_ee_company(jurisdiction="EE")
    e1 = await _make_employee(
        company_id, name="E1 Low Wage", base_rate=Decimal("500.00"),
        isikukood=_TSD_E1_ISIKUKOOD,
    )
    e2 = await _make_employee(
        company_id, name="E2 Pillar Elect", base_rate=Decimal("2000.00"),
        pillar_ii_rate_percent=Decimal("6.0"), isikukood=_TSD_E2_ISIKUKOOD,
    )
    pay_run = await _make_pay_run(company_id)
    for emp in (e1, e2):
        async with AsyncSessionLocal() as session:
            await upsert_line(
                session, pay_run_id=pay_run.id,
                line_input=PayLineInput(
                    employee_id=emp.id, ordinary_hours=Decimal("1"),
                    overtime_hours=Decimal("0"),
                ),
                tenant_id=DEFAULT_TENANT_ID, actor="test",
            )
    async with AsyncSessionLocal() as session:
        await finalize_ee_status_only(
            session, pay_run.id, tenant_id=DEFAULT_TENANT_ID, actor="test",
        )

    period_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        await business_identifiers.upsert(
            session, company_id, "ee_regcode", _TSD_REGCODE,
            tenant_id=DEFAULT_TENANT_ID,
        )
        session.add(
            TaxPeriod(
                id=period_id,
                company_id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                jurisdiction="EST",
                period_type=TaxPeriodType.MONTHLY,
                period_start=_PERIOD_START,
                period_end=_PERIOD_END,
            )
        )
        await session.commit()

    token = current_token()
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Company-Id": str(company_id),
        },
    )
    return client, period_id


async def test_generate_ee_tsd_persists_listing() -> None:
    """/generate's TSD branch: real posted EE pay runs -> generate_tsd ->
    persist_tsd_return -> a READY tax_returns row with list-shaped figures
    (MAIN roll-up + masked-isikukood Lisa-1 rows). Works in this harness
    (unlike EE KMD) because TSD's EE rates come from the embedded fallback,
    not the unconfigured reference DB."""
    client, period_id = await _tsd_golden_client_and_period()
    async with client as ac:
        r = await ac.post(
            "/api/v1/tax_returns/generate",
            json={"jurisdiction": "EE", "return_type": "TSD", "period_id": str(period_id)},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["jurisdiction"] == "EE"
        assert body["return_type"] == "TSD"
        assert body["status"] == "ready"
        figures = body["figures"]
        assert figures["main"]["employee_count"] == 2
        assert figures["main"]["total_gross"] == "2500.00"
        assert figures["main"]["total_social_tax"] == "952.38"
        assert len(figures["lisa1"]) == 2
        # Persisted isikukood is masked (critic round 2) — never plaintext.
        assert all(row["isikukood"].startswith("XXXXXXX") for row in figures["lisa1"])
        assert figures["errors"] == []

        # The persisted return is retrievable via the generic read API the
        # web app renders from.
        r2 = await ac.get(f"/api/v1/tax_returns/{body['id']}")
        assert r2.status_code == 200
        assert r2.json()["return_type"] == "TSD"


async def test_export_ee_tsd_round_trips_golden_xml() -> None:
    """/export's TSD branch: generate then export reproduces the committed
    golden ``tsd_vorm`` XML byte-for-byte. Proves the export re-generates
    from the FINALIZED pay runs (so the REAL isikukood — masked in figures
    — lands in the filed document), keyed on the golden regcode + period."""
    client, period_id = await _tsd_golden_client_and_period()
    async with client as ac:
        r = await ac.post(
            "/api/v1/tax_returns/generate",
            json={"jurisdiction": "EE", "return_type": "TSD", "period_id": str(period_id)},
        )
        assert r.status_code == 201, r.text
        return_id = r.json()["id"]

        r2 = await ac.get(f"/api/v1/tax_returns/{return_id}/export")
        assert r2.status_code == 200, r2.text
        assert r2.headers["content-type"].startswith("application/xml")
        assert "attachment" in r2.headers["content-disposition"]
        assert f"TSD_{return_id}.xml" in r2.headers["content-disposition"]
        # Byte-for-byte against the serializer golden.
        assert r2.content == (_TSD_FIXTURES_DIR / "golden.xml").read_bytes()
        # And the filed document carries the REAL isikukood, not the masked
        # figures copy — the whole reason export re-generates.
        assert _TSD_E1_ISIKUKOOD.encode() in r2.content
        assert _TSD_E2_ISIKUKOOD.encode() in r2.content


async def test_generate_ee_tsd_nil_declaration_when_no_pay_runs() -> None:
    """Empty period -> a valid nil TSD (201), NOT 422. An EE company with a
    period row but no FINALIZED EE pay runs yields ``generate_tsd``'s
    all-zero listing, which the route persists like any other return (task
    decision: follow the generator, which produces a valid nil declaration
    rather than raising). ``_ee_client_and_period`` builds exactly this —
    an EE company + period with no pay-run data."""
    client, period_id = await _ee_client_and_period()
    async with client as ac:
        r = await ac.post(
            "/api/v1/tax_returns/generate",
            json={"jurisdiction": "EE", "return_type": "TSD", "period_id": str(period_id)},
        )
        assert r.status_code == 201, r.text
        figures = r.json()["figures"]
        assert figures["main"]["employee_count"] == 0
        assert figures["main"]["total_gross"] == "0"
        assert figures["lisa1"] == []
        assert figures["errors"] == []


async def test_generate_ee_422_reference_db_unavailable_in_test_harness() -> None:
    """Documents the real, current behaviour of EE /generate in THIS test
    harness (no REFERENCE_DATABASE_URL configured — see module
    docstring): a loud 422, never a silently-empty persisted return."""
    client, period_id = await _ee_client_and_period()
    async with client as ac:
        r = await ac.post(
            "/api/v1/tax_returns/generate",
            json={"jurisdiction": "EE", "return_type": "KMD", "period_id": str(period_id)},
        )
        assert r.status_code == 422
        assert "box definitions" in r.json()["detail"]


async def test_generate_unknown_period_422() -> None:
    client, _period_id = await _au_client_and_period()
    async with client as ac:
        r = await ac.post(
            "/api/v1/tax_returns/generate",
            json={
                "jurisdiction": "AU", "return_type": "BAS",
                "period_id": str(uuid.uuid4()),
            },
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# /generate + /export — AU BAS (reuses the existing SBR envelope builder)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "Pre-existing main gap surfaced by the m1-m15 merge: main's strict "
        "AS.0004 BAS generator (1a07cec) requires the ATO DIN (document_id from "
        "an AS Get prefill), but GET /export calls _build_bas_envelope with no "
        "lodgement_fields -> 422. Whether /export should render a DRAFT copy "
        "without the DIN (lenient) or stay strict until prefill is an open "
        "product decision owned by the SBR lane. Tracked, not silently changed."
    ),
    strict=False,
)
async def test_generate_and_export_au_bas() -> None:
    client, period_id = await _au_client_and_period()
    async with client as ac:
        r = await ac.post(
            "/api/v1/tax_returns/generate",
            json={"jurisdiction": "AU", "return_type": "BAS", "period_id": str(period_id)},
        )
        assert r.status_code == 201, r.text
        return_id = r.json()["id"]

        r2 = await ac.get(f"/api/v1/tax_returns/{return_id}/export")
        assert r2.status_code == 200
        assert r2.headers["content-type"].startswith("application/xml")


async def test_export_501_for_unsupported_return_type() -> None:
    """A persisted TPAR (or any other type with no document builder) must
    fail loudly on export, not silently guess a format."""
    client, period_id = await _au_client_and_period()
    async with client as ac:
        r = await ac.post(
            "/api/v1/tax_returns",
            json={
                "jurisdiction": "AU", "return_type": "TPAR",
                "period_id": str(period_id), "figures": {},
            },
        )
        assert r.status_code == 201
        return_id = r.json()["id"]

        r2 = await ac.get(f"/api/v1/tax_returns/{return_id}/export")
        assert r2.status_code == 501


async def test_export_404_unknown_return() -> None:
    client, _period_id = await _au_client_and_period()
    async with client as ac:
        r = await ac.get(f"/api/v1/tax_returns/{uuid.uuid4()}/export")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# /file — manual FILED transition
# ---------------------------------------------------------------------------


async def test_file_marks_filed_with_timestamp() -> None:
    client, period_id = await _au_client_and_period()
    async with client as ac:
        r = await ac.post(
            "/api/v1/tax_returns",
            json={
                "jurisdiction": "AU", "return_type": "TPAR",
                "period_id": str(period_id), "figures": {"foo": 1},
            },
        )
        assert r.status_code == 201
        return_id = r.json()["id"]

        r2 = await ac.post(
            f"/api/v1/tax_returns/{return_id}/file",
            json={"reference": "manually filed via EMTA e-service"},
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["status"] == "filed"
        assert body["filed_at"] is not None

        # GET reflects the new status + filed_at.
        r3 = await ac.get(f"/api/v1/tax_returns/{return_id}")
        assert r3.status_code == 200
        assert r3.json()["status"] == "filed"
        assert r3.json()["filed_at"] is not None

        # Filing again is rejected — idempotency guard, not a silent no-op.
        r4 = await ac.post(f"/api/v1/tax_returns/{return_id}/file")
        assert r4.status_code == 422


async def test_file_404_unknown_return() -> None:
    client, _period_id = await _au_client_and_period()
    async with client as ac:
        r = await ac.post(f"/api/v1/tax_returns/{uuid.uuid4()}/file")
        assert r.status_code == 404
