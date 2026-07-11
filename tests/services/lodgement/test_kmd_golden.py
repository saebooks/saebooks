"""KMD-formula support Packet 4 — golden-file byte-for-byte serializer test.

Reuses the DB-posting helpers from ``tests/services/test_tax_return_generator.py``
(the Packet-2 domestic golden period + the Packet-3 EU-acquisition
reverse-charge period) — same cross-module import pattern already
established by ``tests/db/test_je_guard_0162.py`` importing from
``test_je_engine_guard.py`` — rather than duplicating ~150 lines of
company/account/tax-code scaffolding. Computes the real 28-box vector via
real posted journal entries + the real aggregator, serializes it, and
byte-compares against the committed fixtures in ``tests/fixtures/kmd/``
(generated once, by hand-verified inspection, from the SAME expected
values these DB-backed tests independently assert in
``test_tax_return_generator.py``).
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest


def _maybe_regen(path: Path, data: bytes) -> None:
    """When SAEBOOKS_REGEN_FIXTURES is set, (re)write the golden fixture from
    the real generator+serializer output before the byte-compare (used to pin
    the fixtures to the real e-MTA schema — run in-container with
    tests/fixtures bind-mounted). No-op in a normal run."""
    if os.environ.get("SAEBOOKS_REGEN_FIXTURES"):
        path.write_bytes(data)

from saebooks.services.lodgement.kmd import (
    KmdFigures,
    KmdReportingContext,
    build_kmd_csv_document,
    build_kmd_xml_document,
)
from tests.services.test_tax_return_generator import (
    _kmd_box_vector,
    _make_ee_company,
    _post_ee_purchase,
    _post_ee_reverse_charge_purchase,
    _post_ee_sale,
)

pytestmark = pytest.mark.postgres_only

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "kmd"
_REGCODE = "10123456"


async def test_kmd_domestic_golden_serialises_byte_for_byte() -> None:
    """The Packet-2 domestic golden period, posted for real, serialised to
    XML/CSV, and byte-compared against the committed fixture."""
    company_id = await _make_ee_company()
    from saebooks.db import AsyncSessionLocal
    from saebooks.models.account import Account
    from saebooks.models.tax_code import TaxCode
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid
            for code, aid in (
                await session.execute(
                    select(Account.code, Account.id).where(Account.company_id == company_id)
                )
            ).all()
        }
        tax_by_type = {
            rt: tid
            for rt, tid in (
                await session.execute(
                    select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id)
                )
            ).all()
        }

    accounts = {
        "bank": by_code["1-1110"], "income": by_code["4-1000"],
        "expense": by_code["5-1000"], "fixed_asset": by_code["1-1500"],
    }
    entry_date = date(2026, 1, 15)

    await _post_ee_sale(company_id, accounts, tax_by_type["standard"], entry_date=entry_date, net=Decimal("10000.00"), gst=Decimal("2400.00"), label="standard 24%")
    await _post_ee_sale(company_id, accounts, tax_by_type["reduced_9"], entry_date=entry_date, net=Decimal("2000.00"), gst=Decimal("180.00"), label="reduced 9%")
    await _post_ee_sale(company_id, accounts, tax_by_type["reduced_13"], entry_date=entry_date, net=Decimal("1000.00"), gst=Decimal("130.00"), label="reduced 13%")
    await _post_ee_sale(company_id, accounts, tax_by_type["zero_export"], entry_date=entry_date, net=Decimal("5000.00"), gst=Decimal("0.00"), label="export")
    await _post_ee_sale(company_id, accounts, tax_by_type["exempt"], entry_date=entry_date, net=Decimal("500.00"), gst=Decimal("0.00"), label="exempt")
    await _post_ee_purchase(company_id, accounts, tax_by_type["standard"], entry_date=entry_date, expense_account_id=accounts["expense"], net=Decimal("2500.00"), gst=Decimal("600.00"), label="standard-rate purchase")
    await _post_ee_purchase(company_id, accounts, tax_by_type["capital"], entry_date=entry_date, expense_account_id=accounts["fixed_asset"], net=Decimal("1000.00"), gst=Decimal("240.00"), label="fixed-asset purchase")

    amounts = await _kmd_box_vector(
        company_id, from_date=date(2026, 1, 1), to_date=date(2026, 1, 31)
    )
    figures = KmdFigures.from_box_amounts(amounts)
    ctx = KmdReportingContext(
        regcode=_REGCODE, period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)
    )

    xml_doc = build_kmd_xml_document(figures, ctx)
    csv_doc = build_kmd_csv_document(figures, ctx)

    _maybe_regen(_FIXTURES_DIR / "domestic_golden.xml", xml_doc)
    _maybe_regen(_FIXTURES_DIR / "domestic_golden.csv", csv_doc)
    assert xml_doc == (_FIXTURES_DIR / "domestic_golden.xml").read_bytes()
    assert csv_doc == (_FIXTURES_DIR / "domestic_golden.csv").read_bytes()


async def test_kmd_rc_eu_acquisition_golden_serialises_byte_for_byte() -> None:
    """The Packet-3 EU-acquisition reverse-charge golden period, posted
    through the real per-jurisdiction dispatcher, serialised, and
    byte-compared against the committed fixture."""
    company_id = await _make_ee_company(jurisdiction="EE")
    from saebooks.db import AsyncSessionLocal
    from saebooks.models.account import Account
    from saebooks.models.tax_code import TaxCode
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        by_code = {
            code: aid
            for code, aid in (
                await session.execute(
                    select(Account.code, Account.id).where(Account.company_id == company_id)
                )
            ).all()
        }
        tax_by_type = {
            rt: tid
            for rt, tid in (
                await session.execute(
                    select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id)
                )
            ).all()
        }

    accounts = {
        "bank": by_code["1-1110"],
        "expense": by_code["5-1000"],
        "vat_rc_payable": by_code["2-1350"],
    }
    await _post_ee_reverse_charge_purchase(
        company_id, accounts, tax_by_type["rc_eu_acq_goods"],
        entry_date=date(2026, 5, 15), net=Decimal("4000.00"),
        self_assessed_vat=Decimal("960.00"), label="EU acquisition of goods",
    )

    amounts = await _kmd_box_vector(
        company_id, from_date=date(2026, 5, 1), to_date=date(2026, 5, 31)
    )
    figures = KmdFigures.from_box_amounts(amounts)
    ctx = KmdReportingContext(
        regcode=_REGCODE, period_start=date(2026, 5, 1), period_end=date(2026, 5, 31)
    )

    xml_doc = build_kmd_xml_document(figures, ctx)
    csv_doc = build_kmd_csv_document(figures, ctx)

    _maybe_regen(_FIXTURES_DIR / "rc_eu_acquisition_golden.xml", xml_doc)
    _maybe_regen(_FIXTURES_DIR / "rc_eu_acquisition_golden.csv", csv_doc)
    assert xml_doc == (_FIXTURES_DIR / "rc_eu_acquisition_golden.xml").read_bytes()
    assert csv_doc == (_FIXTURES_DIR / "rc_eu_acquisition_golden.csv").read_bytes()
