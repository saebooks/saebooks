"""M1.5 · T8 — generic return calculator: box-definition parsing +
reference-DB-driven aggregation + AU BAS thin-wrapper equivalence.

Four groups of tests:

* Pure-unit, no DB — ``_parse_box_definition`` grammar (the aggregation
  string format documented in the AU seed YAML) and the 2-letter ->
  3-letter jurisdiction mapping. Fast, deterministic, catch a typo in
  the parser or a seed row before either ever touches a database.
* ``postgres_only`` — ``generate_return`` reproduces
  ``services.tax_engine.au.bas_report``'s pre-T8 numbers exactly for a
  GST-inclusive taxable sale (the same scenario
  ``tests/services/test_cashbook_bas.py``'s keystone test pins), proving
  the thin-wrapper refactor is behaviour-preserving.
* ``postgres_only`` — with no ``REFERENCE_DATABASE_URL`` configured (the
  standard test/CI shape — see ``docker-compose.test.yml``),
  ``generate_return`` still succeeds via the embedded fallback box set,
  proving AU BAS reporting does not go down when the reference DB is
  absent (MODULARITY — no capability's fault should cascade from an
  unrelated/optional module being unavailable).
* Reference-DB integration (skipped unless both ``REFERENCE_DATABASE_URL``
  and ``REFERENCE_MIGRATION_DATABASE_URL`` are configured, same gate as
  ``tests/integration/test_cross_db_join.py``) — proves
  ``generate_return`` actually READS ``TaxReturnBoxDefinition`` rather
  than hardcoding box literals: a temporary, uniquely-named box row is
  inserted, ``generate_return`` returns it verbatim, and the row is
  removed again in a ``finally``.
"""
from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal, ReferenceSession
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.tax_code import TaxCode
from saebooks.services import journal as journal_svc
from saebooks.services.tax_engine.au import bas_report
from saebooks.services.tax_return_generator import (
    _BoxDefRow,
    _parse_box_definition,
    _to_engine_jurisdiction,
    _to_reference_jurisdiction,
    generate_return,
)

pytestmark = pytest.mark.postgres_only

_FROM = date(2026, 1, 1)
_TO = date(2026, 12, 31)
_ENTRY_DATE = date(2026, 6, 1)


# ---------------------------------------------------------------------------
# Pure-unit — box-definition parsing (no DB; runs even without postgres_only,
# but marked at module level above for consistency with the file's other
# tests; these particular functions don't touch a session).
# ---------------------------------------------------------------------------


def test_parse_box_definition_sum_taxable_inclusive() -> None:
    row = _BoxDefRow("G1", "Total sales", "sum_taxable_for_codes:income:gst_inclusive", ["taxable"], 1)
    parsed = _parse_box_definition(row)
    assert parsed.kind == "sum_taxable_for_codes"
    assert parsed.bucket == "income"
    assert parsed.inclusive is True
    assert parsed.feeder_codes == frozenset({"taxable"})


def test_parse_box_definition_sum_taxable_exclusive() -> None:
    row = _BoxDefRow("G2", "Export sales", "sum_taxable_for_codes:income:gst_exclusive", ["export"], 2)
    parsed = _parse_box_definition(row)
    assert parsed.inclusive is False


def test_parse_box_definition_sum_tax_amount() -> None:
    row = _BoxDefRow("1A", "GST collected", "sum_tax_amount_for_codes:income", ["taxable"], 6)
    parsed = _parse_box_definition(row)
    assert parsed.kind == "sum_tax_amount_for_codes"
    assert parsed.bucket == "income"


def test_parse_box_definition_manual() -> None:
    row = _BoxDefRow("9", "Manual adjustment", "manual", [], 9)
    parsed = _parse_box_definition(row)
    assert parsed.kind == "manual"
    assert parsed.bucket is None


def test_parse_box_definition_formula_not_implemented() -> None:
    row = _BoxDefRow("X1", "Some formula box", "formula:g1-g11", [], 1)
    with pytest.raises(NotImplementedError):
        _parse_box_definition(row)


def test_parse_box_definition_missing_bucket_raises() -> None:
    row = _BoxDefRow("BAD", "Bad box", "sum_taxable_for_codes", ["taxable"], 1)
    with pytest.raises(ValueError, match="income"):
        _parse_box_definition(row)


def test_parse_box_definition_missing_inclusive_modifier_raises() -> None:
    row = _BoxDefRow("BAD", "Bad box", "sum_taxable_for_codes:income", ["taxable"], 1)
    with pytest.raises(ValueError, match="gst_inclusive"):
        _parse_box_definition(row)


def test_parse_box_definition_unknown_kind_raises() -> None:
    row = _BoxDefRow("BAD", "Bad box", "count_the_beans:income", ["taxable"], 1)
    with pytest.raises(ValueError, match="unknown aggregation kind"):
        _parse_box_definition(row)


def test_to_reference_jurisdiction_maps_au_to_aus() -> None:
    assert _to_reference_jurisdiction("AU") == "AUS"
    assert _to_reference_jurisdiction("au") == "AUS"


def test_to_reference_jurisdiction_passthrough_unknown() -> None:
    assert _to_reference_jurisdiction("XX") == "XX"


def test_to_engine_jurisdiction_maps_aus_to_au() -> None:
    """Bug fix (round 6) — the reverse mapping used by the embedded
    fallback lookup must undo _to_reference_jurisdiction so a caller
    supplying the reference-DB-canonical 3-char code ('AUS') still
    resolves to the fallback's 2-letter key ('AU')."""
    assert _to_engine_jurisdiction("AUS") == "AU"
    assert _to_engine_jurisdiction("aus") == "AU"


def test_to_engine_jurisdiction_passthrough_already_engine_code() -> None:
    assert _to_engine_jurisdiction("AU") == "AU"
    assert _to_engine_jurisdiction("au") == "AU"


def test_to_engine_jurisdiction_passthrough_unknown() -> None:
    assert _to_engine_jurisdiction("XX") == "XX"


# ---------------------------------------------------------------------------
# Helpers shared by the DB-backed tests below.
# ---------------------------------------------------------------------------


async def _ctx() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (company_id, asset_account_id, income_account_id, gst_taxcode_id)
    scoped to the shared seed company, mirroring the fixture pattern in
    tests/api/v1/test_reports_bas_cashflow.py."""
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company).where(Company.archived_at.is_(None)).order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None, "seed company not found"

        async def _first_of(t: AccountType) -> uuid.UUID:
            acct = (
                await session.execute(
                    select(Account).where(
                        Account.company_id == company.id,
                        Account.account_type == t,
                        Account.is_header.is_(False),
                    ).order_by(Account.code)
                )
            ).scalars().first()
            assert acct is not None
            return acct.id

        gst_code = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.archived_at.is_(None),
                    TaxCode.code == "GST",
                    TaxCode.tenant_id == DEFAULT_TENANT_ID,
                    TaxCode.company_id == company.id,
                )
            )
        ).scalars().first()
        assert gst_code is not None, "Seed AU tax code GST not found"

        return (
            company.id,
            await _first_of(AccountType.ASSET),
            await _first_of(AccountType.INCOME),
            gst_code.id,
        )


async def _post_gst_sale(
    company_id: uuid.UUID,
    *,
    asset_id: uuid.UUID,
    income_id: uuid.UUID,
    gst_code_id: uuid.UUID,
    net: Decimal,
    gst: Decimal,
) -> None:
    """Dr Asset (net+gst) / Cr Income (net, tax_code_id=GST, gst_amount=gst)."""
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=_ENTRY_DATE,
            description="T8 generate_return test sale",
            lines=[
                {"account_id": asset_id, "debit": net + gst, "credit": Decimal("0")},
                {
                    "account_id": income_id,
                    "debit": Decimal("0"),
                    "credit": net,
                    "tax_code_id": gst_code_id,
                    "gst_amount": gst,
                },
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-t8")


# ---------------------------------------------------------------------------
# generate_return reproduces au.bas_report's pre-T8 numbers (thin-wrapper
# equivalence — the critical acceptance test for this theme).
# ---------------------------------------------------------------------------


async def test_generate_return_matches_au_bas_report_keystone_delta() -> None:
    """A $1,100 GST-inclusive taxable sale must move G1 by 1100 (inclusive)
    and 1A by 100 — the same keystone scenario
    tests/services/test_cashbook_bas.py pins for au.bas_report, now
    reproduced directly against generate_return (both AU's box-definition
    source and au.bas_report's underlying engine, since bas_report is a
    thin wrapper over it as of T8)."""
    company_id, asset_id, income_id, gst_code_id = await _ctx()

    async def _g1_1a() -> tuple[Decimal, Decimal]:
        async with AsyncSessionLocal() as session:
            result = await generate_return(
                session, company_id,
                jurisdiction="AU", return_type="BAS",
                from_date=_FROM, to_date=_TO,
            )
        return result.amount("G1"), result.amount("1A")

    before_g1, before_1a = await _g1_1a()

    await _post_gst_sale(
        company_id, asset_id=asset_id, income_id=income_id,
        gst_code_id=gst_code_id, net=Decimal("1000.00"), gst=Decimal("100.00"),
    )

    after_g1, after_1a = await _g1_1a()

    assert after_g1 - before_g1 == Decimal("1100.00"), (
        "generate_return G1 should move by the GST-inclusive 1100, "
        f"moved by {after_g1 - before_g1}"
    )
    assert after_1a - before_1a == Decimal("100.00"), (
        f"generate_return 1A should move by 100, moved by {after_1a - before_1a}"
    )


async def test_au_bas_report_thin_wrapper_matches_generate_return_directly() -> None:
    """au.bas_report(...) and generate_return(..., jurisdiction='AU',
    return_type='BAS') must agree box-for-box over the same window —
    bas_report is now a thin wrapper over generate_return, so this is a
    direct equivalence check, not just a delta comparison."""
    company_id, asset_id, income_id, gst_code_id = await _ctx()

    await _post_gst_sale(
        company_id, asset_id=asset_id, income_id=income_id,
        gst_code_id=gst_code_id, net=Decimal("500.00"), gst=Decimal("50.00"),
    )

    async with AsyncSessionLocal() as session:
        report = await bas_report(session, company_id, from_date=_FROM, to_date=_TO)
    async with AsyncSessionLocal() as session:
        result = await generate_return(
            session, company_id,
            jurisdiction="AU", return_type="BAS",
            from_date=_FROM, to_date=_TO,
        )

    assert report.g1.amount == result.amount("G1")
    assert report.g2.amount == result.amount("G2")
    assert report.g3.amount == result.amount("G3")
    assert report.g10.amount == result.amount("G10")
    assert report.g11.amount == result.amount("G11")
    assert report.label_1a.amount == result.amount("1A")
    assert report.label_1b.amount == result.amount("1B")
    assert report.g1.description == result.boxes["G1"].box_label


# ---------------------------------------------------------------------------
# Resilience — the standard test/CI environment does NOT configure
# REFERENCE_DATABASE_URL (only REFERENCE_MIGRATION_DATABASE_URL, for
# migrations — see docker-compose.test.yml), so this exercises the
# embedded-fallback path every other BAS test in the suite transitively
# relies on. Explicit here so a regression shows up as a T8-scoped
# failure, not a mysterious BAS-wide outage.
# ---------------------------------------------------------------------------


async def test_generate_return_falls_back_when_reference_db_not_configured() -> None:
    if ReferenceSession is not None:
        pytest.skip(
            "REFERENCE_DATABASE_URL is configured in this environment — "
            "the fallback path isn't exercised here; see "
            "test_generate_return_reads_reference_table_when_configured "
            "for the reference-DB-present case."
        )
    company_id, _asset_id, _income_id, _gst_code_id = await _ctx()
    async with AsyncSessionLocal() as session:
        result = await generate_return(
            session, company_id,
            jurisdiction="AU", return_type="BAS",
            from_date=_FROM, to_date=_TO,
        )
    assert result.source == "embedded_fallback"
    # The fallback box set must still be the full AU BAS box list.
    assert set(result.boxes) == {"G1", "G2", "G3", "G10", "G11", "1A", "1B"}


async def test_generate_return_falls_back_with_reference_db_canonical_jurisdiction_code() -> None:
    """Bug fix (round 6) — a caller supplying the reference-DB's own
    3-char canonical code ('AUS', per models/reference/jurisdiction.py)
    must still hit the embedded fallback, not a spurious 'no box
    definitions' ValueError. Previously the fallback lookup used the
    raw un-mapped jurisdiction, which only ever matched the 2-letter
    engine code.

    Goes beyond a box-codes-only check: posts the same keystone GST
    sale as test_generate_return_matches_au_bas_report_keystone_delta
    and asserts the G1/1A DELTA is identical — proving 'AUS' doesn't
    just avoid the ValueError but produces the correct AU BAS numbers
    (aggregation is company_id/date-scoped, not jurisdiction-filtered,
    so this also guards against a future change coupling the two)."""
    if ReferenceSession is not None:
        pytest.skip(
            "REFERENCE_DATABASE_URL is configured in this environment — "
            "the fallback path isn't exercised here."
        )
    company_id, asset_id, income_id, gst_code_id = await _ctx()

    async def _g1_1a() -> tuple[Decimal, Decimal]:
        async with AsyncSessionLocal() as session:
            result = await generate_return(
                session, company_id,
                jurisdiction="AUS", return_type="BAS",
                from_date=_FROM, to_date=_TO,
            )
        assert result.source == "embedded_fallback"
        assert set(result.boxes) == {"G1", "G2", "G3", "G10", "G11", "1A", "1B"}
        return result.amount("G1"), result.amount("1A")

    before_g1, before_1a = await _g1_1a()

    await _post_gst_sale(
        company_id, asset_id=asset_id, income_id=income_id,
        gst_code_id=gst_code_id, net=Decimal("1000.00"), gst=Decimal("100.00"),
    )

    after_g1, after_1a = await _g1_1a()

    assert after_g1 - before_g1 == Decimal("1100.00"), (
        "generate_return(jurisdiction='AUS') G1 should move by the "
        f"GST-inclusive 1100, moved by {after_g1 - before_g1}"
    )
    assert after_1a - before_1a == Decimal("100.00"), (
        "generate_return(jurisdiction='AUS') 1A should move by 100, "
        f"moved by {after_1a - before_1a}"
    )


# ---------------------------------------------------------------------------
# Proves generate_return actually reads TaxReturnBoxDefinition (not a
# hardcoded box list) — inserts a temporary, uniquely-named box and
# confirms it comes back verbatim. Skipped in the standard test/CI shape
# (same gate as tests/integration/test_cross_db_join.py).
# ---------------------------------------------------------------------------

_reference_db_configured = bool(
    os.environ.get("REFERENCE_DATABASE_URL") and os.environ.get("REFERENCE_MIGRATION_DATABASE_URL")
)


@pytest.mark.skipif(
    not _reference_db_configured,
    reason="REFERENCE_DATABASE_URL / REFERENCE_MIGRATION_DATABASE_URL not both configured",
)
async def test_generate_return_reads_reference_table_when_configured() -> None:
    from saebooks.db import ReferenceMigrationSession
    from saebooks.models.reference.tax_return_box_definition import TaxReturnBoxDefinition
    from saebooks.services.reference.loader import load_seeds

    await load_seeds("AU", version_tag="test-t8")

    company_id, _asset_id, _income_id, _gst_code_id = await _ctx()

    test_box_id = uuid.uuid4()
    assert ReferenceMigrationSession is not None
    async with ReferenceMigrationSession() as ref:
        ref.add(
            TaxReturnBoxDefinition(
                id=test_box_id,
                jurisdiction="AUS",
                return_type="BAS",
                box_code="ZZTEST8",
                box_label="T8 proof-of-read box",
                aggregation="manual",
                feeder_tax_codes=[],
                display_order=999,
            )
        )
        await ref.commit()

    try:
        async with AsyncSessionLocal() as session:
            result = await generate_return(
                session, company_id,
                jurisdiction="AU", return_type="BAS",
                from_date=_FROM, to_date=_TO,
            )
        assert result.source == "reference_db"
        assert "ZZTEST8" in result.boxes, (
            "generate_return did not surface the temporary reference-DB "
            "row — it is not actually reading TaxReturnBoxDefinition"
        )
        assert result.boxes["ZZTEST8"].box_label == "T8 proof-of-read box"
        assert result.boxes["ZZTEST8"].amount == Decimal("0")  # manual box
    finally:
        async with ReferenceMigrationSession() as ref:
            row = await ref.get(TaxReturnBoxDefinition, test_box_id)
            if row is not None:
                await ref.delete(row)
                await ref.commit()


# ---------------------------------------------------------------------------
# Bug fix (round 3) — indirect_tax_rate_percent must pick AU's GST (10%)
# as the "standard vat_gst rate", not excise-type codes (WET 29%, LCT
# 33%) that also live in the AU tax_codes seed with a higher
# rate_percent. Before the fix, WET/LCT had no explicit tax_family in
# the seed YAML, so they defaulted (via the RefTaxCode server_default)
# to 'vat_gst' — the same family as GST — and the highest-rate-wins
# query picked LCT's 33% instead of GST's 10%. The fix tags WET/LCT as
# tax_family='excise' in the seed so the vat_gst filter genuinely
# narrows to GST-family codes only.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _reference_db_configured,
    reason="REFERENCE_DATABASE_URL / REFERENCE_MIGRATION_DATABASE_URL not both configured",
)
async def test_indirect_tax_rate_percent_picks_gst_not_wet_or_lct() -> None:
    from saebooks.services.reference.loader import load_seeds
    from saebooks.services.tax_return_generator import (
        indirect_tax_inclusive_fraction,
        indirect_tax_rate_percent,
    )

    await load_seeds("AU", version_tag="test-t8-rate")

    rate = await indirect_tax_rate_percent("AU")
    assert rate == Decimal("10.0000"), (
        "expected AU's standard vat_gst rate to be GST's 10%, not an "
        "excise-family code (WET 29% / LCT 33%) sharing the vat_gst "
        f"family via an untagged seed row; got {rate}"
    )

    fraction = await indirect_tax_inclusive_fraction("AU")
    assert fraction == Decimal("10.0000") / Decimal("110.0000")
