"""CSV bulk-import tests for the Fixed Asset Register (Batch MM/2).

Two layers:

1. Pure parse tests — exercise ``parse_assets_csv`` in isolation
   (header problems raise, row-level problems land on the row).
2. Classify + apply tests — hit the live seed DB to verify
   DB-resolution (account codes + dep model) and the idempotent
   ``(company_id, code)`` skip behaviour.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account
from saebooks.models.company import Company
from saebooks.models.fixed_asset import FixedAsset
from saebooks.services import assets_import as imp
from saebooks.services.assets_import import AssetImportError
pytestmark = pytest.mark.postgres_only

# ---------------------------------------------------------------------- #
# Sample CSV fixtures                                                    #
# ---------------------------------------------------------------------- #

_MINIMAL_HEADER = (
    "code,name,purchase_date,cost,depreciation_model_id,"
    "cost_account_code,accum_dep_account_code"
)

_MINIMAL_ROW = (
    "FA-IMP-0001,Test laptop,2026-04-01,2500.00,asset_3_year_linear,"
    "1-3310,1-3320"
)


# ---------------------------------------------------------------------- #
# Pure parse layer                                                       #
# ---------------------------------------------------------------------- #


def test_parse_minimal_happy_path() -> None:
    raw = f"{_MINIMAL_HEADER}\n{_MINIMAL_ROW}\n"
    rows = imp.parse_assets_csv(raw)
    assert len(rows) == 1
    r = rows[0]
    assert r.errors == ()
    assert r.code == "FA-IMP-0001"
    assert r.name == "Test laptop"
    assert r.purchase_date == date(2026, 4, 1)
    assert r.cost == Decimal("2500.00")
    assert r.depreciation_model_id == "asset_3_year_linear"
    assert r.cost_account_code == "1-3310"
    assert r.accum_dep_account_code == "1-3320"
    # In-service defaults to purchase_date when blank.
    assert r.in_service_date == date(2026, 4, 1)


def test_parse_missing_header_raises() -> None:
    with pytest.raises(AssetImportError):
        imp.parse_assets_csv("")


def test_parse_missing_required_column_raises() -> None:
    # Header is fine but ``cost`` is missing.
    raw = (
        "code,name,purchase_date,depreciation_model_id,"
        "cost_account_code,accum_dep_account_code\n"
    )
    with pytest.raises(AssetImportError, match="cost"):
        imp.parse_assets_csv(raw)


def test_parse_case_insensitive_and_whitespace_tolerant_header() -> None:
    raw = (
        "  Code ,Name, Purchase_Date ,Cost,DEPRECIATION_MODEL_ID,"
        "cost_account_code,accum_dep_account_code\n"
        f"{_MINIMAL_ROW}\n"
    )
    rows = imp.parse_assets_csv(raw)
    assert len(rows) == 1
    assert rows[0].errors == ()
    assert rows[0].code == "FA-IMP-0001"


def test_parse_accepts_australian_date_format() -> None:
    raw = (
        f"{_MINIMAL_HEADER}\n"
        "FA-IMP-AU,Australian date,01/04/2026,500,asset_3_year_linear,"
        "1-3310,1-3320\n"
    )
    rows = imp.parse_assets_csv(raw)
    assert rows[0].errors == ()
    assert rows[0].purchase_date == date(2026, 4, 1)


def test_parse_row_with_bad_date_gets_error() -> None:
    raw = (
        f"{_MINIMAL_HEADER}\n"
        "FA-IMP-BAD,Bad date,not-a-date,500,asset_3_year_linear,"
        "1-3310,1-3320\n"
    )
    rows = imp.parse_assets_csv(raw)
    assert len(rows) == 1
    assert any("purchase_date" in e for e in rows[0].errors)


def test_parse_row_with_blank_required_fields_gets_errors() -> None:
    raw = (
        f"{_MINIMAL_HEADER}\n"
        ",,,,,,\n"
    )
    rows = imp.parse_assets_csv(raw)
    r = rows[0]
    # Expect multiple required-field errors (the parser short-circuits
    # ``purchase_date`` / ``cost`` "required" messages once any earlier
    # error has been appended — avoids spamming the error table — so we
    # just check the string-field required errors surface).
    joined = " | ".join(r.errors)
    assert "code" in joined
    assert "name" in joined
    assert "depreciation_model_id" in joined
    assert "cost_account_code" in joined
    assert "accum_dep_account_code" in joined


def test_parse_negative_cost_rejected() -> None:
    raw = (
        f"{_MINIMAL_HEADER}\n"
        "FA-IMP-NEG,Bad cost,2026-04-01,-500,asset_3_year_linear,"
        "1-3310,1-3320\n"
    )
    rows = imp.parse_assets_csv(raw)
    assert any("> 0" in e for e in rows[0].errors)


def test_parse_picks_up_optional_columns() -> None:
    header = (
        _MINIMAL_HEADER
        + ",in_service_date,residual_value,dep_expense_account_code,"
        "description,serial_number,manufacturer,model_number,"
        "location,custody_person,warranty_end"
    )
    row = (
        _MINIMAL_ROW
        + ",2026-05-01,100.00,6-1500,A description,SN-1,Dell,XPS,"
        "Desk 3,Alice,2029-04-01"
    )
    rows = imp.parse_assets_csv(f"{header}\n{row}\n")
    r = rows[0]
    assert r.errors == ()
    assert r.in_service_date == date(2026, 5, 1)
    assert r.residual_value == Decimal("100.00")
    assert r.dep_expense_account_code == "6-1500"
    assert r.description == "A description"
    assert r.serial_number == "SN-1"
    assert r.manufacturer == "Dell"
    assert r.model_number == "XPS"
    assert r.location == "Desk 3"
    assert r.custody_person == "Alice"
    assert r.warranty_end == date(2029, 4, 1)


def test_parse_lineno_tracks_header_is_line_one() -> None:
    raw = (
        f"{_MINIMAL_HEADER}\n"
        f"{_MINIMAL_ROW}\n"
        f"{_MINIMAL_ROW.replace('0001', '0002')}\n"
    )
    rows = imp.parse_assets_csv(raw)
    assert [r.lineno for r in rows] == [2, 3]


# ---------------------------------------------------------------------- #
# Classify + apply (DB-backed)                                           #
# ---------------------------------------------------------------------- #


async def _first_company_id() -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        company = (
            await session.execute(
                select(Company)
                .where(Company.archived_at.is_(None))
                .order_by(Company.created_at)
            )
        ).scalars().first()
        assert company is not None
        return company.id


@pytest.fixture(autouse=True)
async def _scrub_import_assets() -> AsyncGenerator[None, None]:
    """Drop any ``FA-IMP-*`` rows left behind by a prior run.

    The tests share the live seed DB + use deterministic codes (so the
    idempotency test can re-run them). Without a pre-test wipe, the
    ``skip`` bucket ends up holding rows from the last run and the
    assertions on new-row counts break.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(FixedAsset).where(FixedAsset.code.like("FA-IMP-%"))
        )
        await session.commit()
    yield
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(FixedAsset).where(FixedAsset.code.like("FA-IMP-%"))
        )
        await session.commit()


async def test_classify_all_new_rows() -> None:
    company_id = await _first_company_id()
    raw = f"{_MINIMAL_HEADER}\n{_MINIMAL_ROW}\n"
    rows = imp.parse_assets_csv(raw)
    async with AsyncSessionLocal() as session:
        plan = await imp.classify_rows(session, company_id, rows)
    assert len(plan.create) == 1
    assert plan.skip == []
    assert plan.invalid == []


async def test_classify_flags_unknown_account_code() -> None:
    company_id = await _first_company_id()
    raw = (
        f"{_MINIMAL_HEADER}\n"
        "FA-IMP-BADACCT,Unknown account,2026-04-01,100,asset_3_year_linear,"
        "9-9999,1-3320\n"
    )
    rows = imp.parse_assets_csv(raw)
    async with AsyncSessionLocal() as session:
        plan = await imp.classify_rows(session, company_id, rows)
    assert len(plan.invalid) == 1
    assert any("cost_account_code" in e for e in plan.invalid[0].errors)


async def test_classify_flags_unknown_depreciation_model() -> None:
    company_id = await _first_company_id()
    raw = (
        f"{_MINIMAL_HEADER}\n"
        "FA-IMP-BADMODEL,Unknown model,2026-04-01,100,not_a_real_model,"
        "1-3310,1-3320\n"
    )
    rows = imp.parse_assets_csv(raw)
    async with AsyncSessionLocal() as session:
        plan = await imp.classify_rows(session, company_id, rows)
    assert len(plan.invalid) == 1
    assert any("depreciation_model_id" in e for e in plan.invalid[0].errors)


async def test_apply_import_writes_rows() -> None:
    company_id = await _first_company_id()
    raw = f"{_MINIMAL_HEADER}\n{_MINIMAL_ROW}\n"
    rows = imp.parse_assets_csv(raw)
    async with AsyncSessionLocal() as session:
        plan = await imp.classify_rows(session, company_id, rows)
        written = await imp.apply_import(session, company_id, plan)
        await session.commit()
    assert written == 1

    async with AsyncSessionLocal() as session:
        persisted = (
            await session.execute(
                select(FixedAsset).where(
                    FixedAsset.company_id == company_id,
                    FixedAsset.code == "FA-IMP-0001",
                )
            )
        ).scalars().one()
        assert persisted.name == "Test laptop"
        assert persisted.cost == Decimal("2500.00")
        assert persisted.depreciation_model_id == "asset_3_year_linear"


async def test_apply_import_is_idempotent_on_rerun() -> None:
    company_id = await _first_company_id()
    raw = f"{_MINIMAL_HEADER}\n{_MINIMAL_ROW}\n"

    # First run — creates the row.
    async with AsyncSessionLocal() as session:
        plan1 = await imp.classify_rows(
            session, company_id, imp.parse_assets_csv(raw)
        )
        await imp.apply_import(session, company_id, plan1)
        await session.commit()
    assert len(plan1.create) == 1

    # Second run — same CSV should skip, not duplicate.
    async with AsyncSessionLocal() as session:
        plan2 = await imp.classify_rows(
            session, company_id, imp.parse_assets_csv(raw)
        )
        written = await imp.apply_import(session, company_id, plan2)
        await session.commit()
    assert plan2.create == []
    assert len(plan2.skip) == 1
    assert plan2.skip[0].code == "FA-IMP-0001"
    assert written == 0

    # And only one FixedAsset row with that code exists.
    async with AsyncSessionLocal() as session:
        count = (
            await session.execute(
                select(FixedAsset).where(
                    FixedAsset.company_id == company_id,
                    FixedAsset.code == "FA-IMP-0001",
                )
            )
        ).scalars().all()
    assert len(count) == 1


async def test_apply_import_default_dep_expense_account() -> None:
    """Blank dep_expense_account_code falls back to ``6-1500``."""
    company_id = await _first_company_id()
    raw = f"{_MINIMAL_HEADER}\n{_MINIMAL_ROW}\n"
    rows = imp.parse_assets_csv(raw)
    async with AsyncSessionLocal() as session:
        plan = await imp.classify_rows(session, company_id, rows)
        await imp.apply_import(session, company_id, plan)
        await session.commit()

    async with AsyncSessionLocal() as session:
        asset = (
            await session.execute(
                select(FixedAsset).where(
                    FixedAsset.company_id == company_id,
                    FixedAsset.code == "FA-IMP-0001",
                )
            )
        ).scalars().one()
        # Resolve the 6-1500 account to compare by id.
        dep_acct = (
            await session.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.code == "6-1500",
                )
            )
        ).scalar_one()
        assert asset.dep_expense_account_id == dep_acct.id
