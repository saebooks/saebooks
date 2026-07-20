"""UK VAT100 golden-file test — a synthetic UK company posts one
quarter of real entries (including a domestic-reverse-charge
construction purchase, a PVA import, an NI-protocol EU acquisition and
dispatch, and an international-services reverse charge) through the
REAL per-jurisdiction dispatch path (services.journal.post ->
_apply_tax_treatment -> get_engine("UK") -> UKTaxEngine.
compute_components -> JournalLineTaxComponent rows), then reads ALL
nine statutory boxes back through the real box-set.

Why this drives ``_aggregate_ledger_by_box`` + ``_evaluate_formula_boxes``
directly instead of ``generate_return(jurisdiction="UK", ...)``: the
exact reason the EE KMD goldens in test_tax_return_generator.py do —
``REFERENCE_DATABASE_URL`` is never configured in this harness, and
``_FALLBACK_BOX_DEFINITIONS`` only carries AU/BAS (by design; UK, like
EE, is reference-DB-only). The REAL UK seed YAML (the byte-identical
source the loader applies) supplies the parsed box set; only the
reference-DB row *fetch* is bypassed, which the AU-generic
reference-DB integration test already covers.

Hand-computed expectations (2026 Q2, GBP):

    sales:    standard 10,000 + 2,000 VAT; reduced 1,000 + 50;
              zero 500; exempt 200; XI dispatch 800
    purchases: standard 3,000 + 600 VAT; DRC construction 2,000
              (self-assessed 400); intl services RC 1,200 (240);
              PVA import 5,000 (1,000); XI acquisition 1,500 (300)

    Box 1 = 2,050 (domestic) + 400 + 240 + 1,000 (RC/PVA) = 3,690.00
    Box 2 = 300.00 (XI acquisition VAT)
    Box 3 = 3,990.00
    Box 4 = 600 (domestic) + 400 + 240 + 1,000 + 300 (RC legs) = 2,540.00
    Box 5 = 1,450.00 (|3,990 - 2,540|)
    Box 6 = 12,500 (sales incl. exempt + DRC-supply-less dispatch 800)
            + 1,200 (RC services received — 700/12: value in 6 AND 7)
            = 13,700.00
    Box 7 = 3,000 + 2,000 + 1,200 + 5,000 + 1,500 = 12,700.00
    Box 8 = 800.00
    Box 9 = 1,500.00
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

_UK_SEED_PATH = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "UK"
    / "tax_return_box_definitions.yaml"
)

_EXPECTED = {
    "1": Decimal("3690.00"),
    "2": Decimal("300.00"),
    "3": Decimal("3990.00"),
    "4": Decimal("2540.00"),
    "5": Decimal("1450.00"),
    "6": Decimal("13700.00"),
    "7": Decimal("12700.00"),
    "8": Decimal("800.00"),
    "9": Decimal("1500.00"),
    # Internal legs, pinned too — a feeder collision that shifts an
    # amount between legs while keeping the statutory total right
    # should still fail loudly.
    "1_DOMESTIC": Decimal("2050.00"),
    "1_RC": Decimal("1640.00"),
    "4_DOMESTIC": Decimal("600.00"),
    "4_RC": Decimal("1940.00"),
    "6_DOMESTIC": Decimal("12500.00"),
    "6_RC_SERVICES": Decimal("1200.00"),
}


def _uk_vat100_parsed_boxes():
    doc = yaml.safe_load(_UK_SEED_PATH.read_text())
    rows = [r for r in doc["rows"] if r["return_type"] == "VAT100"]
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


async def _make_uk_company() -> uuid.UUID:
    """Throwaway UK company (own chart, own UK tax codes) — every box
    below is an absolute value, so isolation from other tests' postings
    matters (the EE golden's own rationale)."""
    company_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        session.add(
            Company(
                id=company_id,
                tenant_id=DEFAULT_TENANT_ID,
                name=f"VAT100 Golden {company_id.hex[:8]}",
                base_currency="GBP",
                fin_year_start_month=4,
                audit_mode="immutable",
                jurisdiction="UK",
            )
        )
        await session.flush()

        # Same GLOBAL gst auto-post settings convention as every other
        # golden (test_cashbook_bas / EE KMD) — idempotent alongside them.
        await settings_svc.set(session, "gst_collected_account_code", "2-1310")
        await settings_svc.set(session, "gst_paid_account_code", "2-1330")
        await settings_svc.set(session, "gst_auto_post", "true")

        accounts = {
            "bank": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="1-1110", name="Bank", account_type=AccountType.ASSET),
            "income": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="4-1000", name="Sales", account_type=AccountType.INCOME),
            "expense": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="5-1000", name="Purchases", account_type=AccountType.EXPENSE),
            "vat_collected": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1310", name="VAT on sales", account_type=AccountType.LIABILITY),
            "vat_paid": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1330", name="VAT on purchases", account_type=AccountType.ASSET),
            # Output-side self-assessed liability for reverse-charge/PVA
            # postings — booked explicitly (auto_post_gst_lines adds only
            # the single INPUT-side line for an expense-bucket line; the
            # EE RC golden's account comment explains why the output side
            # is manual).
            "vat_rc_payable": Account(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="2-1350", name="VAT self-assessed (reverse charge/PVA)", account_type=AccountType.LIABILITY),
        }
        for acct in accounts.values():
            session.add(acct)
        await session.flush()

        tax_codes = {
            "standard": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="UK-STD", name="UK 20%", rate=Decimal("20.000"), tax_system="VAT", jurisdiction="UK", reporting_type="standard"),
            "reduced": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="UK-RED", name="UK 5%", rate=Decimal("5.000"), tax_system="VAT", jurisdiction="UK", reporting_type="reduced"),
            "zero": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="UK-ZERO", name="UK 0%", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="UK", reporting_type="zero"),
            "exempt": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="UK-EXEMPT", name="UK exempt", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="UK", reporting_type="exempt"),
            "rc_construction": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="UK-DRC", name="UK DRC construction (20%)", rate=Decimal("20.000"), tax_system="VAT", jurisdiction="UK", reporting_type="rc_construction"),
            "rc_services_intl": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="UK-RCSVC", name="UK intl services RC (20%)", rate=Decimal("20.000"), tax_system="VAT", jurisdiction="UK", reporting_type="rc_services_intl"),
            "pva_import": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="UK-PVA", name="UK PVA import (20%)", rate=Decimal("20.000"), tax_system="VAT", jurisdiction="UK", reporting_type="pva_import"),
            "xi_eu_acq_goods": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="UK-XIACQ", name="XI EU acquisition (20%)", rate=Decimal("20.000"), tax_system="VAT", jurisdiction="UK", reporting_type="xi_eu_acq_goods"),
            "xi_eu_dispatch": TaxCode(company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="UK-XIDSP", name="XI EU dispatch (0%)", rate=Decimal("0.000"), tax_system="VAT", jurisdiction="UK", reporting_type="xi_eu_dispatch"),
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
            description=f"VAT100 golden — {label}",
            lines=[
                {"account_id": accounts["bank"], "debit": net + vat, "credit": Decimal("0")},
                {"account_id": accounts["income"], "debit": Decimal("0"), "credit": net, "tax_code_id": tax_code_id, "gst_amount": vat},
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-vat100-golden")


async def _post_purchase(company_id, accounts, tax_code_id, *, entry_date, net, vat, label):
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"VAT100 golden — {label}",
            lines=[
                {"account_id": accounts["expense"], "debit": net, "credit": Decimal("0"), "tax_code_id": tax_code_id, "gst_amount": vat},
                {"account_id": accounts["bank"], "debit": Decimal("0"), "credit": net + vat},
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-vat100-golden")


async def _post_reverse_charge_purchase(
    company_id, accounts, tax_code_id, *, entry_date, net, self_assessed_vat, label
):
    """DR expense net (tax-tagged -> UKTaxEngine fans out output+input
    components AND auto_post_gst_lines adds the input-side VAT line);
    CR bank net (the supplier is paid net — no VAT charged to us);
    CR vat_rc_payable vat (the explicit output-side self-assessed
    liability). Balances: DR net + vat == CR net + vat. The exact EE
    RC-golden posting shape."""
    async with AsyncSessionLocal() as session:
        entry = await journal_svc.create_draft(
            session,
            company_id=company_id,
            tenant_id=DEFAULT_TENANT_ID,
            entry_date=entry_date,
            description=f"VAT100 golden RC — {label}",
            lines=[
                {"account_id": accounts["expense"], "debit": net, "credit": Decimal("0"), "tax_code_id": tax_code_id, "gst_amount": self_assessed_vat},
                {"account_id": accounts["bank"], "debit": Decimal("0"), "credit": net},
                {"account_id": accounts["vat_rc_payable"], "debit": Decimal("0"), "credit": self_assessed_vat},
            ],
        )
        await journal_svc.post(session, entry.id, posted_by="pytest-vat100-rc")


async def test_uk_vat100_golden_quarter_all_nine_boxes() -> None:
    company_id = await _make_uk_company()
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

    period_from, period_to = date(2026, 4, 1), date(2026, 6, 30)
    d = date(2026, 5, 15)

    await _post_sale(company_id, accounts, tax_by_type["standard"], entry_date=d, net=Decimal("10000.00"), vat=Decimal("2000.00"), label="standard sale")
    await _post_sale(company_id, accounts, tax_by_type["reduced"], entry_date=d, net=Decimal("1000.00"), vat=Decimal("50.00"), label="reduced sale")
    await _post_sale(company_id, accounts, tax_by_type["zero"], entry_date=d, net=Decimal("500.00"), vat=Decimal("0.00"), label="zero sale")
    await _post_sale(company_id, accounts, tax_by_type["exempt"], entry_date=d, net=Decimal("200.00"), vat=Decimal("0.00"), label="exempt sale")
    await _post_sale(company_id, accounts, tax_by_type["xi_eu_dispatch"], entry_date=d, net=Decimal("800.00"), vat=Decimal("0.00"), label="XI dispatch")

    await _post_purchase(company_id, accounts, tax_by_type["standard"], entry_date=d, net=Decimal("3000.00"), vat=Decimal("600.00"), label="standard purchase")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["rc_construction"], entry_date=d, net=Decimal("2000.00"), self_assessed_vat=Decimal("400.00"), label="DRC construction")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["rc_services_intl"], entry_date=d, net=Decimal("1200.00"), self_assessed_vat=Decimal("240.00"), label="intl services RC")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["pva_import"], entry_date=d, net=Decimal("5000.00"), self_assessed_vat=Decimal("1000.00"), label="PVA import")
    await _post_reverse_charge_purchase(company_id, accounts, tax_by_type["xi_eu_acq_goods"], entry_date=d, net=Decimal("1500.00"), self_assessed_vat=Decimal("300.00"), label="XI EU acquisition")

    parsed = _uk_vat100_parsed_boxes()
    async with AsyncSessionLocal() as session:
        ledger_amounts = await _aggregate_ledger_by_box(
            session, parsed,
            company_id=company_id, tenant_id=None,
            from_date=period_from, to_date=period_to,
            statuses=(EntryStatus.POSTED,), exclude_archived=False,
        )
    amounts = _evaluate_formula_boxes(parsed, ledger_amounts, return_type="VAT100")

    for box_code, expected in _EXPECTED.items():
        assert amounts.get(box_code) == expected, (
            f"VAT100 box {box_code!r} expected {expected}, got {amounts.get(box_code)}"
        )
