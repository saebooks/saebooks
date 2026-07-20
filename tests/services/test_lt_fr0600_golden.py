"""LT FR0600 golden-file test — a synthetic LT company posts one month
of real entries (including an intra-EU goods acquisition, an Art 95
EU-services reverse charge, a non-EU services reverse charge, an
Art 96 domestic-reverse-charge construction purchase, an import-VAT
purchase, a seller-side Art 96 supply, and every sales rate bucket)
through the REAL per-jurisdiction dispatch path (services.journal.post
-> _apply_tax_treatment -> get_engine("LT") -> LTTaxEngine.
compute_components -> JournalLineTaxComponent rows), then reads the
statutory boxes back through the real box-set.

Why this drives ``_aggregate_ledger_by_box`` + ``_evaluate_formula_boxes``
directly instead of ``generate_return(jurisdiction="LT", ...)``: the
exact reason the EE KMD and UK VAT100 goldens do —
``REFERENCE_DATABASE_URL`` is never configured in this harness, and
``_FALLBACK_BOX_DEFINITIONS`` only carries AU/BAS (by design; LT, like
EE/UK, is reference-DB-only). The REAL LT seed YAML (the byte-identical
source the loader applies) supplies the parsed box set.

Hand-computed expectations (2026-05, EUR):

    sales:     standard 10,000 + 2,100 VAT; reduced-12 1,000 + 120;
               reduced-5 500 + 25; exempt 200; export 800;
               IC supply 1,500; other-zero 300; outside-LT 400;
               Art 96 supply (seller side) 2,500
    purchases: standard 3,000 + 630 VAT; import 1,000 + 210 VAT paid
               at customs; DRC construction 2,000 (self-assessed 420);
               EU goods acquisition 5,000 (1,050); EU services 1,200
               (252); non-EU services 800 (168)

    Box 11  = 10,000 + 1,000 + 500                     = 11,500.00
    Box 12  = 2,500.00      Box 13 = 200.00
    Box 17  = 800.00        Box 18 = 1,500.00    Box 19 = 300.00
    Box 20  = 400.00        Box 21 = 5,000.00
    Box 23  = 1,200 + 800                              =  2,000.00
    Box 24  = 1,200.00
    Box 25  = 630 (domestic) + 420 + 1,050 + 252 + 168 (RC legs)
                                                       =  2,520.00
    Box 26  = 210.00        Box 27 = 0 (manual, absent)
    Box 29  = 2,100.00      Box 29A = 120.00
    Box 30  = 0.00 (no legacy-9% postings)  Box 31 = 25.00
    Box 32  = 252 + 168                                =    420.00
    Box 33  = 420.00        Box 34 = 1,050.00
    Box 35  = 25 + 26 + 27 = 2,520 + 210 + 0           =  2,730.00
    Box 36  = 2,100 + 120 + 0 + 25 + 420 + 420 + 1,050 + 0 - 2,730
                                                       =  1,405.00
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.journal import EntryStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services import journal as journal_svc
from saebooks.services import settings as settings_svc
from saebooks.services.tax_return_generator import (
    _aggregate_ledger_by_box,
    _BoxDefRow,
    _evaluate_formula_boxes,
    _parse_box_definition,
)

pytestmark = pytest.mark.postgres_only

_LT_SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "LT"
    / "tax_return_box_definitions.yaml"
)

_EXPECTED = {
    "11": Decimal("11500.00"),
    "12": Decimal("2500.00"),
    "13": Decimal("200.00"),
    "17": Decimal("800.00"),
    "18": Decimal("1500.00"),
    "19": Decimal("300.00"),
    "20": Decimal("400.00"),
    "21": Decimal("5000.00"),
    "23": Decimal("2000.00"),
    "24": Decimal("1200.00"),
    "25": Decimal("2520.00"),
    "26": Decimal("210.00"),
    "29": Decimal("2100.00"),
    "29A": Decimal("120.00"),
    "30": Decimal("0.00"),
    "31": Decimal("25.00"),
    "32": Decimal("420.00"),
    "33": Decimal("420.00"),
    "34": Decimal("1050.00"),
    "35": Decimal("2730.00"),
    "36": Decimal("1405.00"),
    # Internal legs, pinned too — a feeder collision that shifts an
    # amount between legs while keeping the statutory total right
    # should still fail loudly (the UK golden's rationale).
    "25_DOMESTIC": Decimal("630.00"),
    "25_RC": Decimal("1890.00"),
}


def _lt_fr0600_parsed_boxes():
    doc = yaml.safe_load(_LT_SEED_PATH.read_text())
    rows = [r for r in doc["rows"] if r["return_type"] == "FR0600"]
    return [
        _parse_box_definition(
            _BoxDefRow(
                box_code=r["box_code"],
                box_label=r["box_label"],
                aggregation=r["aggregation"],
                feeder_tax_codes=r.get("feeder_tax_codes") or [],
                display_order=r["display_order"],
                formula=r.get("formula"),
            )
        )
        for r in rows
    ]


async def _make_lt_company() -> uuid.UUID:
    """Throwaway LT company (own chart, own LT tax codes) — every box
    below is an absolute value, so isolation from other tests' postings
    matters (the EE/UK goldens' rationale)."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                name=f"FR0600 Golden {company_id.hex[:8]}",
                base_currency="EUR",
                fin_year_start_month=1,
                audit_mode="immutable",
                jurisdiction="LT",
            )
        )
        await session.flush()

        # Same GLOBAL gst auto-post settings convention as every other
        # golden — idempotent alongside them.
        await settings_svc.set(session, "gst_collected_account_code", "2-1310")
        await settings_svc.set(session, "gst_paid_account_code", "2-1330")
        await settings_svc.set(session, "gst_auto_post", "true")

        accounts = {
            "bank": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="1-1110", name="Bank", account_type=AccountType.ASSET),
            "income": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="4-1000", name="Sales", account_type=AccountType.INCOME),
            "expense": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="5-1000", name="Purchases", account_type=AccountType.EXPENSE),
            "vat_collected": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1310", name="PVM on sales", account_type=AccountType.LIABILITY),
            "vat_paid": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1330", name="PVM on purchases", account_type=AccountType.ASSET),
            # Output-side self-assessed liability for reverse-charge
            # postings — booked explicitly (auto_post_gst_lines adds only
            # the single INPUT-side line for an expense-bucket line; the
            # EE/UK RC goldens' account comment explains why the output
            # side is manual).
            "vat_rc_payable": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1350", name="PVM self-assessed (reverse charge)", account_type=AccountType.LIABILITY),
        }
        for acct in accounts.values():
            session.add(acct)
        await session.flush()

        tax_codes = {
            "standard": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-STD", name="PVM 21%", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LT", reporting_type="standard"),
            "reduced_12": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-RED12", name="PVM 12%", rate=Decimal("12.000"), tax_system="VAT", jurisdiction="LT", reporting_type="reduced_12"),
            "reduced_5": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-RED5", name="PVM 5%", rate=Decimal("5.000"), tax_system="VAT", jurisdiction="LT", reporting_type="reduced_5"),
            "exempt": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-EXEMPT", name="PVM exempt", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="LT", reporting_type="exempt"),
            "zero_export": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-EXP0", name="Export 0%", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="LT", reporting_type="zero_export"),
            "zero_ic_goods": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-IC0", name="IC supply 0%", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="LT", reporting_type="zero_ic_goods"),
            "zero_other": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-ZOTH", name="Other 0%", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="LT", reporting_type="zero_other"),
            "outside_lt": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-OUT", name="Outside LT", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="LT", reporting_type="outside_lt"),
            "rc_domestic_supply": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-RCS", name="Art 96 supply", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="LT", reporting_type="rc_domestic_supply"),
            "rc_domestic_acq": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-RCA", name="Art 96 acquisition (21%)", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LT", reporting_type="rc_domestic_acq"),
            "rc_eu_acq_goods": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-EUG", name="EU goods acquisition (21%)", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LT", reporting_type="rc_eu_acq_goods"),
            "rc_eu_acq_services": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-EUS", name="EU services Art 95 (21%)", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LT", reporting_type="rc_eu_acq_services"),
            "rc_services_noneu": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-NEUS", name="Non-EU services Art 95 (21%)", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LT", reporting_type="rc_services_noneu"),
            "input_import": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LT-IMP", name="Import VAT (21%)", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LT", reporting_type="input_import"),
        }
        for tc in tax_codes.values():
            session.add(tc)
        await session.commit()

    return company_id


async def _post_sale(company_id, accounts, tax_code_id, *, entry_date, net, vat, label):
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"FR0600 golden — {label}",
            lines=[
                {"account_id": accounts["bank"], "debit": net + vat, "credit": Decimal("0")},
                {"account_id": accounts["income"], "debit": Decimal("0"), "credit": net, "tax_code_id": tax_code_id, "gst_amount": vat},
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-fr0600-golden")


async def _post_purchase(company_id, accounts, tax_code_id, *, entry_date, net, vat, label):
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"FR0600 golden — {label}",
            lines=[
                {"account_id": accounts["expense"], "debit": net, "credit": Decimal("0"), "tax_code_id": tax_code_id, "gst_amount": vat},
                {"account_id": accounts["bank"], "debit": Decimal("0"), "credit": net + vat},
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-fr0600-golden")


async def _post_reverse_charge_purchase(
    company_id, accounts, tax_code_id, *, entry_date, net, self_assessed_vat, label
):
    """DR expense net (tax-tagged -> LTTaxEngine fans out output+input
    components AND auto_post_gst_lines adds the input-side VAT line);
    CR bank net (the supplier is paid net — no VAT charged to us);
    CR vat_rc_payable vat (the explicit output-side self-assessed
    liability). The exact EE/UK RC-golden posting shape."""
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"FR0600 golden RC — {label}",
            lines=[
                {"account_id": accounts["expense"], "debit": net, "credit": Decimal("0"), "tax_code_id": tax_code_id, "gst_amount": self_assessed_vat},
                {"account_id": accounts["bank"], "debit": Decimal("0"), "credit": net},
                {"account_id": accounts["vat_rc_payable"], "debit": Decimal("0"), "credit": self_assessed_vat},
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-fr0600-rc")


async def test_lt_fr0600_golden_month_all_boxes() -> None:
    company_id = await _make_lt_company()
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
        "income": by_code["4-1000"],
        "expense": by_code["5-1000"],
        "vat_rc_payable": by_code["2-1350"],
    }

    period_from, period_to = date(2026, 5, 1), date(2026, 5, 31)
    d = date(2026, 5, 15)

    # -- sales -----------------------------------------------------------
    await _post_sale(company_id, accounts, tax_by_type["standard"], entry_date=d, net=Decimal("10000.00"), vat=Decimal("2100.00"), label="standard sale")
    await _post_sale(company_id, accounts, tax_by_type["reduced_12"], entry_date=d, net=Decimal("1000.00"), vat=Decimal("120.00"), label="12% sale")
    await _post_sale(company_id, accounts, tax_by_type["reduced_5"], entry_date=d, net=Decimal("500.00"), vat=Decimal("25.00"), label="5% sale")
    await _post_sale(company_id, accounts, tax_by_type["exempt"], entry_date=d, net=Decimal("200.00"), vat=Decimal("0.00"), label="exempt sale")
    await _post_sale(company_id, accounts, tax_by_type["zero_export"], entry_date=d, net=Decimal("800.00"), vat=Decimal("0.00"), label="export")
    await _post_sale(company_id, accounts, tax_by_type["zero_ic_goods"], entry_date=d, net=Decimal("1500.00"), vat=Decimal("0.00"), label="IC supply")
    await _post_sale(company_id, accounts, tax_by_type["zero_other"], entry_date=d, net=Decimal("300.00"), vat=Decimal("0.00"), label="other zero")
    await _post_sale(company_id, accounts, tax_by_type["outside_lt"], entry_date=d, net=Decimal("400.00"), vat=Decimal("0.00"), label="outside LT")
    await _post_sale(company_id, accounts, tax_by_type["rc_domestic_supply"], entry_date=d, net=Decimal("2500.00"), vat=Decimal("0.00"), label="Art 96 supply (seller)")

    # -- purchases ---------------------------------------------------------
    await _post_purchase(company_id, accounts, tax_by_type["standard"], entry_date=d, net=Decimal("3000.00"), vat=Decimal("630.00"), label="standard purchase")
    await _post_purchase(company_id, accounts, tax_by_type["input_import"], entry_date=d, net=Decimal("1000.00"), vat=Decimal("210.00"), label="import (VAT paid at customs)")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["rc_domestic_acq"], entry_date=d, net=Decimal("2000.00"), self_assessed_vat=Decimal("420.00"), label="Art 96 construction")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["rc_eu_acq_goods"], entry_date=d, net=Decimal("5000.00"), self_assessed_vat=Decimal("1050.00"), label="EU goods acquisition")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["rc_eu_acq_services"], entry_date=d, net=Decimal("1200.00"), self_assessed_vat=Decimal("252.00"), label="EU services Art 95")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["rc_services_noneu"], entry_date=d, net=Decimal("800.00"), self_assessed_vat=Decimal("168.00"), label="non-EU services Art 95")

    parsed = _lt_fr0600_parsed_boxes()
    async with AsyncSessionLocal() as session:
        ledger_amounts = await _aggregate_ledger_by_box(
            session, parsed,
            company_id=company_id, tenant_id=None,
            from_date=period_from, to_date=period_to,
            statuses=(EntryStatus.POSTED,), exclude_archived=False,
        )
    amounts = _evaluate_formula_boxes(parsed, ledger_amounts, return_type="FR0600")

    for box_code, expected in _EXPECTED.items():
        assert amounts.get(box_code) == expected, (
            f"FR0600 box {box_code!r} expected {expected}, got {amounts.get(box_code)}"
        )
