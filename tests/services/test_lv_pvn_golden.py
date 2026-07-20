"""LV PVN declaration golden-file test — a synthetic Latvian company
posts one month of real entries (including EU reverse-charge
acquisitions of goods AND services, a third-country services reverse
charge, a seller-side domestic reverse-charge supply, import VAT, and
the exempt/zero splits) through the REAL per-jurisdiction dispatch path
(services.journal.post -> _apply_tax_treatment -> get_engine("LV") ->
LVTaxEngine.compute_components -> JournalLineTaxComponent rows), then
reads EVERY declaration row back through the real seed's box set.

Why this drives ``_aggregate_ledger_by_box`` + ``_evaluate_formula_boxes``
directly instead of ``generate_return(jurisdiction="LV", ...)``: the
exact reason the EE KMD and UK VAT100 goldens do —
``REFERENCE_DATABASE_URL`` is never configured in this harness, and
``_FALLBACK_BOX_DEFINITIONS`` only carries AU/BAS (by design; LV, like
EE/NZ/UK, is reference-DB-only). The REAL LV seed YAML supplies the
parsed box set; only the reference-DB row *fetch* is bypassed.

Hand-computed expectations (2026-03, EUR):

    sales:     standard 10,000 + 2,100 VAT; reduced12 1,000 + 120;
               reduced5 500 + 25; domestic-RC supply (timber, seller
               side) 1,500; IC goods dispatch 800; export 600;
               exempt 200
    purchases: domestic standard 3,000 + 630 VAT; import VAT 150 (net
               1,000); EU goods acquisition 2,000 (self-assessed 420);
               EU services 1,000 (210); third-country services 400 (84)

    41=10,000  41.1=1,500  42=1,000  42.1=500
    43 = 45(800) + 48.1(600) = 1,400          49 = 200   48.2 = 0
    40 = 10,000+1,500+1,000+500+1,400+0+200 = 14,600
    50 = 2,000 + 1,000 = 3,000   (goods AND EU services — row 50's
                                  verbatim text includes both)
    52 = 0.21×41 = 2,100   53 = 120   53.1 = 25
    54 = 84   55 = 0.21×50 = 630   56 = 0   56.1 = 0
    S  = 2,100+120+25+84+630 = 2,959
    61 = 150   62 = 630   63 = 84   64 = 420+210 = 630
    60 = 150+630+84+630+0 = 1,494   P = 1,494
    80 = 2,959 − 1,494 = 1,465   70 = 0
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

_LV_SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "LV"
    / "tax_return_box_definitions.yaml"
)

_EXPECTED = {
    "40": Decimal("14600.00"),
    "41": Decimal("10000.00"),
    "41.1": Decimal("1500.00"),
    "42": Decimal("1000.00"),
    "42.1": Decimal("500.00"),
    "43": Decimal("1400.00"),
    "44": Decimal("0"),
    "45": Decimal("800.00"),
    "45.1": Decimal("0"),
    "46": Decimal("0"),
    "47": Decimal("0"),
    "48": Decimal("0"),
    "48.1": Decimal("600.00"),
    "48.2": Decimal("0"),
    "49": Decimal("200.00"),
    "50": Decimal("3000.00"),
    "51": Decimal("0"),
    "51.1": Decimal("0"),
    "52": Decimal("2100.00"),
    "53": Decimal("120.00"),
    "53.1": Decimal("25.00"),
    "54": Decimal("84.00"),
    "55": Decimal("630.00"),
    "56": Decimal("0.00"),
    "56.1": Decimal("0.00"),
    "57": Decimal("0"),
    "60": Decimal("1494.00"),
    "61": Decimal("150.00"),
    "62": Decimal("630.00"),
    "63": Decimal("84.00"),
    "64": Decimal("630.00"),
    "65": Decimal("0"),
    "66": Decimal("0"),
    "67": Decimal("0"),
    "S": Decimal("2959.00"),
    "P": Decimal("1494.00"),
    "80": Decimal("1465.00"),
    "70": Decimal("0.00"),
}


def _lv_pvn_parsed_boxes():
    doc = yaml.safe_load(_LV_SEED_PATH.read_text())
    rows = [r for r in doc["rows"] if r["return_type"] == "PVN"]
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


async def _make_lv_company() -> uuid.UUID:
    """Throwaway LV company (own chart, own LV tax codes) — every row
    below is an absolute value, so isolation from other tests' postings
    matters (the EE/UK goldens' rationale)."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                name=f"PVN Golden {company_id.hex[:8]}",
                base_currency="EUR",
                fin_year_start_month=1,
                audit_mode="immutable",
                jurisdiction="LV",
            )
        )
        await session.flush()

        # Same GLOBAL gst auto-post settings convention as the other
        # goldens — idempotent alongside them.
        await settings_svc.set(session, "gst_collected_account_code", "2-1310")
        await settings_svc.set(session, "gst_paid_account_code", "2-1330")
        await settings_svc.set(session, "gst_auto_post", "true")

        accounts = {
            "bank": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="1-1110", name="Banka", account_type=AccountType.ASSET),
            "income": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="4-1000", name="Ieņēmumi", account_type=AccountType.INCOME),
            "expense": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="5-1000", name="Izmaksas", account_type=AccountType.EXPENSE),
            "vat_collected": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1310", name="PVN par pārdošanu", account_type=AccountType.LIABILITY),
            "vat_paid": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1330", name="Priekšnodoklis", account_type=AccountType.ASSET),
            # Output-side self-assessed liability for reverse-charge
            # postings — booked explicitly (auto_post_gst_lines adds only
            # the INPUT-side line for an expense-bucket line; the EE/UK
            # RC goldens' account comment explains why).
            "vat_rc_payable": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1350", name="PVN pašaprēķins (apgrieztā maksāšana)", account_type=AccountType.LIABILITY),
        }
        for acct in accounts.values():
            session.add(acct)
        await session.flush()

        tax_codes = {
            "standard": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-STD", name="PVN 21%", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LV", reporting_type="standard"),
            "reduced_12": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-RED12", name="PVN 12%", rate=Decimal("12.000"), tax_system="VAT", jurisdiction="LV", reporting_type="reduced_12"),
            "reduced_5": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-RED5", name="PVN 5%", rate=Decimal("5.000"), tax_system="VAT", jurisdiction="LV", reporting_type="reduced_5"),
            "rc_domestic_supply": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-RCDOM", name="Apgrieztā maksāšana (pārdevējs)", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="LV", reporting_type="rc_domestic_supply"),
            "zero_ic_goods": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-ICG", name="Piegāde ES 0%", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="LV", reporting_type="zero_ic_goods"),
            "zero_export": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-EXP", name="Eksports 0%", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="LV", reporting_type="zero_export"),
            "exempt": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-EXEMPT", name="Neapliekams (52.p.)", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="LV", reporting_type="exempt"),
            "input_import": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-IMP", name="Importa PVN", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LV", reporting_type="input_import"),
            "rc_eu_acq_goods": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-RCEUG", name="Preču iegāde ES (21%)", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LV", reporting_type="rc_eu_acq_goods"),
            "rc_eu_acq_services": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-RCEUS", name="ES pakalpojumi (21%)", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LV", reporting_type="rc_eu_acq_services"),
            "rc_third_country_services": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="LV-RC3C", name="Trešo valstu pakalpojumi (21%)", rate=Decimal("21.000"), tax_system="VAT", jurisdiction="LV", reporting_type="rc_third_country_services"),
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
            description=f"PVN golden — {label}",
            lines=[
                {"account_id": accounts["bank"], "debit": net + vat, "credit": Decimal("0")},
                {"account_id": accounts["income"], "debit": Decimal("0"), "credit": net, "tax_code_id": tax_code_id, "gst_amount": vat},
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-pvn-golden")


async def _post_purchase(company_id, accounts, tax_code_id, *, entry_date, net, vat, label):
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"PVN golden — {label}",
            lines=[
                {"account_id": accounts["expense"], "debit": net, "credit": Decimal("0"), "tax_code_id": tax_code_id, "gst_amount": vat},
                {"account_id": accounts["bank"], "debit": Decimal("0"), "credit": net + vat},
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-pvn-golden")


async def _post_reverse_charge_purchase(
    company_id, accounts, tax_code_id, *, entry_date, net, self_assessed_vat, label
):
    """DR expense net (tax-tagged -> LVTaxEngine fans out output+input
    components AND auto_post_gst_lines adds the input-side VAT line);
    CR bank net (the supplier is paid net); CR vat_rc_payable vat (the
    explicit output-side self-assessed liability). The exact EE/UK
    RC-golden posting shape."""
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"PVN golden RC — {label}",
            lines=[
                {"account_id": accounts["expense"], "debit": net, "credit": Decimal("0"), "tax_code_id": tax_code_id, "gst_amount": self_assessed_vat},
                {"account_id": accounts["bank"], "debit": Decimal("0"), "credit": net},
                {"account_id": accounts["vat_rc_payable"], "debit": Decimal("0"), "credit": self_assessed_vat},
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-pvn-rc")


async def test_lv_pvn_golden_month_every_row() -> None:
    company_id = await _make_lv_company()
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

    period_from, period_to = date(2026, 3, 1), date(2026, 3, 31)
    d = date(2026, 3, 16)

    await _post_sale(company_id, accounts, tax_by_type["standard"], entry_date=d, net=Decimal("10000.00"), vat=Decimal("2100.00"), label="standard sale")
    await _post_sale(company_id, accounts, tax_by_type["reduced_12"], entry_date=d, net=Decimal("1000.00"), vat=Decimal("120.00"), label="reduced 12 sale")
    await _post_sale(company_id, accounts, tax_by_type["reduced_5"], entry_date=d, net=Decimal("500.00"), vat=Decimal("25.00"), label="reduced 5 sale")
    await _post_sale(company_id, accounts, tax_by_type["rc_domestic_supply"], entry_date=d, net=Decimal("1500.00"), vat=Decimal("0.00"), label="domestic RC supply (timber, seller side)")
    await _post_sale(company_id, accounts, tax_by_type["zero_ic_goods"], entry_date=d, net=Decimal("800.00"), vat=Decimal("0.00"), label="IC goods dispatch")
    await _post_sale(company_id, accounts, tax_by_type["zero_export"], entry_date=d, net=Decimal("600.00"), vat=Decimal("0.00"), label="export")
    await _post_sale(company_id, accounts, tax_by_type["exempt"], entry_date=d, net=Decimal("200.00"), vat=Decimal("0.00"), label="exempt sale")

    await _post_purchase(company_id, accounts, tax_by_type["standard"], entry_date=d, net=Decimal("3000.00"), vat=Decimal("630.00"), label="standard purchase")
    await _post_purchase(company_id, accounts, tax_by_type["input_import"], entry_date=d, net=Decimal("1000.00"), vat=Decimal("150.00"), label="import VAT")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["rc_eu_acq_goods"], entry_date=d, net=Decimal("2000.00"), self_assessed_vat=Decimal("420.00"), label="EU goods acquisition")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["rc_eu_acq_services"], entry_date=d, net=Decimal("1000.00"), self_assessed_vat=Decimal("210.00"), label="EU services")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["rc_third_country_services"], entry_date=d, net=Decimal("400.00"), self_assessed_vat=Decimal("84.00"), label="third-country services")

    parsed = _lv_pvn_parsed_boxes()
    async with AsyncSessionLocal() as session:
        ledger_amounts = await _aggregate_ledger_by_box(
            session, parsed,
            company_id=company_id, tenant_id=None,
            from_date=period_from, to_date=period_to,
            statuses=(EntryStatus.POSTED,), exclude_archived=False,
        )
    amounts = _evaluate_formula_boxes(parsed, ledger_amounts, return_type="PVN")

    for box_code, expected in _EXPECTED.items():
        assert amounts.get(box_code) == expected, (
            f"PVN row {box_code!r} expected {expected}, got {amounts.get(box_code)}"
        )
