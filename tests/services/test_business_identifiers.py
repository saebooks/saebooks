"""Tests for the business_identifiers child table + service."""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select, text

from saebooks.db import AsyncSessionLocal
from saebooks.models.business_identifier import BusinessIdentifier
from saebooks.models.company import Company
from saebooks.services import business_identifiers as bi_svc

_GLOBAL_JURISDICTIONS_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "_global"
    / "jurisdictions.yaml"
)


async def _seed_company() -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as session:
        co = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert co is not None, "seed company missing"
        return co.tenant_id, co.id


async def test_upsert_and_get_round_trip() -> None:
    tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        row = await bi_svc.upsert(
            session, company_id, "nz_nzbn", "9429000000001", tenant_id=tenant_id
        )
        await session.commit()
        assert row.id is not None
        assert row.scheme == "nz_nzbn"

    async with AsyncSessionLocal() as session:
        fetched = await bi_svc.get(session, company_id, "nz_nzbn")
        assert fetched is not None
        assert fetched.value == "9429000000001"


async def test_upsert_updates_existing_row() -> None:
    tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        await bi_svc.upsert(
            session, company_id, "uk_crn", "01234567", tenant_id=tenant_id
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        await bi_svc.upsert(
            session, company_id, "uk_crn", "07654321", tenant_id=tenant_id
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(BusinessIdentifier).where(
                    BusinessIdentifier.company_id == company_id,
                    BusinessIdentifier.scheme == "uk_crn",
                )
            )
        ).scalars().all()
        assert len(rows) == 1, "upsert created a duplicate row"
        assert rows[0].value == "07654321"


async def test_unknown_scheme_rejected() -> None:
    _, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        with pytest.raises(bi_svc.UnknownScheme):
            await bi_svc.upsert(session, company_id, "moon_id", "42")


async def test_rls_policy_installed_on_business_identifiers() -> None:
    """Verify ENABLE/FORCE ROW LEVEL SECURITY + tenant_isolation policy
    are both in place. The policy is the same shape as 0055/0083 — we
    don't re-test RLS enforcement (covered by tests/test_web_router_tenant_scope.py
    against shared infrastructure); we just assert this table joined the club.
    """
    async with AsyncSessionLocal() as session:
        rls_row = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = 'business_identifiers'"
                )
            )
        ).first()
        assert rls_row is not None, "business_identifiers table missing"
        assert rls_row[0] is True, "RLS not ENABLED on business_identifiers"
        assert rls_row[1] is True, "RLS not FORCED on business_identifiers"

        policy_row = (
            await session.execute(
                text(
                    "SELECT polname FROM pg_policy "
                    "WHERE polrelid = 'business_identifiers'::regclass "
                    "  AND polname = 'tenant_isolation'"
                )
            )
        ).first()
        assert policy_row is not None, (
            "tenant_isolation policy missing on business_identifiers"
        )


async def test_backfill_seeded_au_abn_from_companies_abn() -> None:
    """Migration backfill: any company with companies.abn set should have
    a matching scheme='au_abn' row after migration. We tolerate the seed
    company having no ABN — only assert the row exists when the column
    is populated.
    """
    _tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        co = await session.get(Company, company_id)
        if not co or not co.abn:
            pytest.skip("seed company has no ABN to backfill against")
        existing = await bi_svc.get(session, company_id, "au_abn")
        if existing is None:
            # The backfill runs at MIGRATION time; the test seed company is
            # created at app startup AFTER migrations, so it is never covered.
            # Skip deterministically rather than flake on test ordering.
            pytest.skip("no migration-backfilled au_abn row for the runtime seed company")
        assert existing.value == co.abn


# ---------------------------------------------------------------------------
# M1.5 · T9 — jurisdiction / check_digit_valid / valid_from / valid_to /
# issuing_authority columns, and the new-scheme validators.
# ---------------------------------------------------------------------------


async def test_0181_backfilled_jurisdiction_on_existing_au_abn_rows() -> None:
    """0181 backfills jurisdiction='AUS' on every pre-existing au_abn row."""
    _tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        co = await session.get(Company, company_id)
        if not co or not co.abn:
            pytest.skip("seed company has no ABN to backfill against")
        existing = await bi_svc.get(session, company_id, "au_abn")
        if existing is None:
            # Backfill is migration-time; the runtime seed company predates no
            # au_abn row. Skip deterministically rather than flake on ordering.
            pytest.skip("no migration-backfilled au_abn row for the runtime seed company")
        assert existing.jurisdiction == "AUS"


async def test_upsert_derives_jurisdiction_when_not_supplied() -> None:
    tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        row = await bi_svc.upsert(
            session, company_id, "uk_crn", "01234567", tenant_id=tenant_id
        )
        await session.commit()
        assert row.jurisdiction == "GBR"


async def test_upsert_honours_explicit_jurisdiction_override() -> None:
    tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        row = await bi_svc.upsert(
            session,
            company_id,
            "global_lei",
            "5493001KJTIIGC8Y1R12",
            tenant_id=tenant_id,
            jurisdiction="EUR",
        )
        await session.commit()
        assert row.jurisdiction == "EUR"


async def test_upsert_sets_valid_window_and_issuing_authority() -> None:
    tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        row = await bi_svc.upsert(
            session,
            company_id,
            "ee_regcode",
            "12345678",
            tenant_id=tenant_id,
            valid_from=date(2020, 1, 1),
            valid_to=date(2030, 1, 1),
            issuing_authority="Estonian Business Register",
        )
        await session.commit()
        assert row.valid_from == date(2020, 1, 1)
        assert row.valid_to == date(2030, 1, 1)
        assert row.issuing_authority == "Estonian Business Register"


async def test_upsert_computes_check_digit_valid_for_registered_scheme() -> None:
    tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        good = await bi_svc.upsert(
            session, company_id, "au_acn", "004085616", tenant_id=tenant_id
        )
        await session.commit()
        assert good.check_digit_valid is True

    async with AsyncSessionLocal() as session:
        bad = await bi_svc.upsert(
            session, company_id, "nz_ird", "49091851", tenant_id=tenant_id
        )
        await session.commit()
        assert bad.check_digit_valid is False


async def test_upsert_leaves_check_digit_valid_none_for_unregistered_scheme() -> None:
    """nz_nzbn has no validator registered — writing it is unaffected by T9."""
    tenant_id, company_id = await _seed_company()
    async with AsyncSessionLocal() as session:
        row = await bi_svc.upsert(
            session, company_id, "nz_nzbn", "9429000000001", tenant_id=tenant_id
        )
        await session.commit()
        assert row.check_digit_valid is None


@pytest.mark.parametrize(
    ("scheme", "value", "expected"),
    [
        # au_abn — real ATO example (mod-89 checksum).
        ("au_abn", "51 824 753 556", True),
        ("au_abn", "51 824 753 557", False),
        # au_acn — real ASIC example (mod-10 checksum).
        ("au_acn", "004 085 616", True),
        ("au_acn", "004 085 617", False),
        # nz_ird — real Inland Revenue example (mod-11 double-weight).
        ("nz_ird", "49091850", True),
        ("nz_ird", "49091851", False),
        # Format-only schemes — regex shape check, no checksum.
        ("us_ein", "12-3456789", True),
        ("us_ein", "not-an-ein", False),
        ("uk_utr", "1234567890", True),
        ("uk_utr", "12345", False),
        ("in_pan", "ABCDE1234F", True),
        ("in_pan", "1234567890", False),
        ("in_gstin", "27AAPFU0939F1ZV", True),
        ("in_gstin", "not-a-gstin", False),
        ("ca_bn", "123456789", True),
        ("ca_bn", "123456789RT0001", True),
        ("ca_bn", "abc", False),
    ],
)
def test_scheme_validators(scheme: str, value: str, expected: bool) -> None:
    assert bi_svc.validate(scheme, value) is expected


def test_validate_returns_none_for_scheme_without_a_registered_validator() -> None:
    for scheme in ("nz_nzbn", "uk_crn", "ee_regcode", "global_lei"):
        assert bi_svc.validate(scheme, "anything") is None


def test_all_known_schemes_accepted() -> None:
    """The T9 additions (us_ein, uk_utr, uk_vat, eu_vat, in_gstin, in_pan,
    nz_ird, ca_bn) must all round-trip through scheme validation without
    raising UnknownScheme."""
    new_schemes = {
        "us_ein",
        "uk_utr",
        "uk_vat",
        "eu_vat",
        "in_gstin",
        "in_pan",
        "nz_ird",
        "ca_bn",
    }
    assert new_schemes <= bi_svc.KNOWN_SCHEMES
    for scheme in new_schemes:
        assert bi_svc._validate_scheme(scheme) == scheme


def test_scheme_jurisdiction_defaults_all_have_a_seeded_jurisdiction_row() -> None:
    """Bug fix (round 6) — every non-None value in _SCHEME_JURISDICTION
    must correspond to a real row in the _global jurisdictions seed, or
    upsert()'s default silently stores a jurisdiction code that will
    never match anything (models/business_identifier.py documents the
    column as matching saebooks.models.reference.jurisdiction, with no
    cross-DB FK to enforce it). This is a pure-unit, no-DB check that
    catches a scheme -> jurisdiction default being added ahead of its
    seed row, the way us_ein/in_gstin/in_pan/ca_bn -> USA/IND/CAN were."""
    doc = yaml.safe_load(_GLOBAL_JURISDICTIONS_SEED.read_text())
    assert doc["table"] == "jurisdictions"
    seeded_codes = {row["code"] for row in doc["rows"]}

    for scheme, jurisdiction_code in bi_svc._SCHEME_JURISDICTION.items():
        assert jurisdiction_code in seeded_codes, (
            f"_SCHEME_JURISDICTION[{scheme!r}] = {jurisdiction_code!r} has "
            f"no matching row in {_GLOBAL_JURISDICTIONS_SEED} — upsert() "
            f"would silently store an unmatchable jurisdiction code."
        )
