"""KMD 2027 — VALUE-LEVEL GOLDEN against EMTA's own worked example.

Ported+adapted from the parallel ``feat/kmd3-2027`` (``kmd_apa``) golden harness
to drive the CANONICAL ``generate_kmd_2027`` generator. Reconstructs the source
data behind ``APA_KMD_lahteandmed_20260507.xlsx`` (the 20 numbered examples
underlying ``tests/fixtures/xbrl_gl_ee_2027/sample.xml``) as POSTED ledger
records, runs the real generator, and diffs the result against ``sample.xml``
ITSELF (parsed live — not a hand-transcribed expectation table). Then serializes
the reconstructed period and validates it against the REAL XSD schema set.

HONEST OUTCOME — the reproduction headline is the primary economic triple per
row: ``(kmdtyyp_code == accountSubID, amount, tax_rate)``. Secondary fields
diverge in documented, valid ways — RE-DERIVED for the canonical generator,
which differs from the donor in three deliberate producibility boundaries
(``generator.py`` docstring):

  * **identifier_category 103 not emitted (rows 7, 18).** The sample drops the
    partner code for < €1,000 partners (category 103, optional). The canonical
    generator ALWAYS transmits the real code at category 100 (a valid stricter
    superset; the per-partner-period €1,000 test is deferred). ⇒ category 100
    where the sample shows 103; partner_code present where the sample omits it.
  * **country subaccount omitted (row 8, M_201).** The RTK2T2013ap partner-
    country accountSub needs a Contact country field the model lacks; omitted
    (an optional block). ⇒ ``country_code is None`` where the sample shows FI.
  * **credit-invoice original date + bill-side ARVE_KOGUSUMMA omitted (rows 13,
    15).** ``original_invoice_dates`` needs the CreditNote→Invoice link; bill-
    side ``invoice_total`` needs a confirmed ex-VAT column. Both optional; both
    omitted.

Conversely the canonical generator is a SUPERSET on the sale side: it always
emits ``invoice_total`` (ARVE_KOGUSUMMA = ex-VAT subtotal), ``document_date`` and
an ``identifierReference`` on sale rows where the sample omits them — all valid
optional blocks. None of these change the economic triple.

PACKET 3 closed the two former model-gap rows (2, 19) — see ``generator.py``'s
docstring for how: row 2's ``documentApplyToNumber`` is a field-swap on the
invoice's own row (POSTED incoming Payment allocated to it, dated before the
invoice's own ``issue_date``); row 19 is a ``SupplierCreditNote`` posted through
the same purchase-side resolution as an ordinary bill, signed negative.

PRODUCIBILITY BUCKETS (15 produced + 5 no-mapping = 20):

1. PRODUCED (15): 1, 2, 5, 6, 7, 8, 10, 12, 13, 14, 15, 16, 17, 18, 19 —
   economic triple diffed against the parsed sample. Rows 14+17 come from the
   reverse-charge fan-out (each rc_eu_acq_goods Bill emits an S_101 base + O_401
   input row), so two Bills reproduce the sample's two independent amounts and
   emit two documented "other half" extras (S_101 300, O_401 600).
2. NO MAPPING (5): 3, 4, 9, 11, 20 — M_103/M_104 margin scheme, M_206 IC excise,
   M_210 other-0%, O_601 input-VAT correction: UNMAPPED in kmdtyyp_mapping.yaml
   (engine: []). Asserted ABSENT from the output. See ``docs/kmd-2027-scope.md``
   for the seed's ``engine: []`` rationale per leaf and what building each one
   would take.

DB-bound: postgres_only.
"""
from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from lxml import etree
from sqlalchemy import select

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.tax_code import TaxCode
from saebooks.models.contact import ContactType
from saebooks.services import invoices as invoices_svc
from saebooks.services import payments as payments_svc
from saebooks.services import settings as settings_svc
from saebooks.services import supplier_credit_notes as supplier_credit_notes_svc
from saebooks.services.lodgement.kmd_2027 import mapping as m
from saebooks.services.lodgement.kmd_2027.generator import generate_kmd_2027
from saebooks.services.lodgement.kmd_2027.serializer import (
    Kmd2027ReportingContext,
    build_kmd_2027_xml_document,
)
from tests.services.lodgement._xbrl_gl_validation import validate_against_xsd
from tests.services.lodgement.test_kmd_inf_generator import (
    _contact,
    _post_bill,
    _post_credit_note,
    _post_invoice,
)
from tests.services.test_tax_return_generator import _make_ee_company

pytestmark = pytest.mark.postgres_only

_D = Decimal
_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "xbrl_gl_ee_2027"
_SAMPLE_PATH = _FIXTURES_DIR / "sample.xml"

_PERIOD_START = date(2027, 1, 1)
_PERIOD_END = date(2027, 1, 31)

# Bucket 1 (produced), bucket 2 (no engine mapping).
_PRODUCED = (1, 2, 5, 6, 7, 8, 10, 12, 13, 14, 15, 16, 17, 18, 19)
_NO_MAPPING_CODES = {"M_103", "M_104", "M_206", "M_210", "O_601"}   # rows 3,4,9,11,20

_GL_NS = {
    "gl-cor": "http://www.xbrl.org/int/gl/cor/2015-03-25",
    "gl-bus": "http://www.xbrl.org/int/gl/bus/2015-03-25",
}


def _t(el, path: str) -> str | None:
    found = el.find(path, _GL_NS) if el is not None else None
    return found.text if found is not None else None


def _parse_sample_rows(path: Path) -> dict[int, dict]:
    """Parse ``sample.xml``'s ``entryDetail`` rows, keyed by ``lineNumberCounter``
    (== the sample's own example number)."""
    tree = etree.parse(str(path))
    rows: dict[int, dict] = {}
    for d in tree.findall(".//gl-cor:entryDetail", _GL_NS):
        no = int(_t(d, "gl-cor:lineNumberCounter"))
        subs = d.findall("gl-cor:account/gl-cor:accountSub", _GL_NS)
        rate = _t(d, "gl-cor:taxes/gl-cor:taxPercentageRate")
        ident = d.find("gl-cor:identifierReference", _GL_NS)
        rows[no] = dict(
            account_sub_id=_t(subs[0], "gl-cor:accountSubID") if subs else None,
            country=_t(subs[1], "gl-cor:accountSubID") if len(subs) > 1 else None,
            amount=Decimal(_t(d, "gl-cor:amount")),
            rate=Decimal(rate) if rate is not None else None,
            identifier_category=_t(ident, "gl-cor:identifierCategory"),
            identifier_code=_t(ident, "gl-cor:identifierCode"),
            identifier_description=_t(ident, "gl-cor:identifierDescription"),
        )
    return rows


async def _company_with_all_codes() -> tuple[uuid.UUID, dict, dict]:
    """``_make_ee_company`` (EE) + AR/AP control accounts + the three extra
    reporting_type tax codes the 20 examples need that the shared helper does not
    seed (rc_domestic_supply, zero_ic_goods, input_import), + the RC-payable
    setting rows 14/17 need to post."""
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        session.add(Account(company_id=company_id, code="1200", name="Trade Debtors", account_type=AccountType.ASSET))
        session.add(Account(company_id=company_id, code="2100", name="Trade Creditors", account_type=AccountType.LIABILITY))
        session.add(TaxCode(company_id=company_id, code="EE-RC-DOM-SUP", name="EE domestic RC supply 24%", rate=_D("24.000"), tax_system="VAT", jurisdiction="EE", reporting_type="rc_domestic_supply"))
        session.add(TaxCode(company_id=company_id, code="EE-ZIC-GOODS", name="EE IC supply of goods 0%", rate=_D("0.000"), tax_system="VAT", jurisdiction="EE", reporting_type="zero_ic_goods"))
        session.add(TaxCode(company_id=company_id, code="EE-IMPORT", name="EE import VAT 9%", rate=_D("9.000"), tax_system="VAT", jurisdiction="EE", reporting_type="input_import"))
        # bills_svc RC guard — required before any rc_eu_acq_goods Bill can post.
        await settings_svc.set(session, "gst_reverse_charge_payable_account_code", "2-1350")
        await session.commit()

    async with AsyncSessionLocal() as session:
        by_code = {c: i for c, i in (await session.execute(select(Account.code, Account.id).where(Account.company_id == company_id))).all()}
        tax = {rt: tid for rt, tid in (await session.execute(select(TaxCode.reporting_type, TaxCode.id).where(TaxCode.company_id == company_id))).all()}
    return company_id, by_code, tax


async def _post_prepaid_invoice(
    company_id, contact_id, income_account_id, tax_code_id, bank_account_id, *,
    net: Decimal, invoice_issue_date: date, payment_date: date,
) -> None:
    """Post an invoice, then a POSTED incoming Payment allocated to it whose
    ``payment_date`` PREDATES the invoice's own ``issue_date`` — the field-swap
    trigger ``generator.py``'s ``_prepayment_invoice_ids`` keys off (sample
    row 2). ``payments.allocate`` requires the invoice already POSTED, so the
    invoice must be posted first; the payment's own (backdated) date is what
    carries the "prepayment" signal, not posting order."""
    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=invoice_issue_date, due_date=invoice_issue_date,
            currency="EUR",
            lines=[{
                "description": "KMD 2027 golden prepayment-settled sale",
                "account_id": income_account_id, "tax_code_id": tax_code_id,
                "quantity": Decimal("1"), "unit_price": net,
            }],
        )
        posted = await invoices_svc.post_invoice(session, inv.id, posted_by="pytest-kmd-2027")
        invoice_id, invoice_total = posted.id, posted.total

    async with AsyncSessionLocal() as session:
        pay = await payments_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            bank_account_id=bank_account_id, payment_date=payment_date,
            amount=invoice_total, currency="EUR",
        )
        await payments_svc.allocate(session, pay.id, invoice_allocations=[(invoice_id, invoice_total)])
        await payments_svc.post_payment(session, pay.id, posted_by="pytest-kmd-2027")


async def _post_supplier_credit_note(
    company_id, contact_id, expense_account_id, tax_code_id, *,
    net: Decimal, issue_date: date,
) -> None:
    async with AsyncSessionLocal() as session:
        scn = await supplier_credit_notes_svc.api_create(
            session, company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
            actor="pytest-kmd-2027", contact_id=contact_id, issue_date=issue_date,
            lines=[{
                "description": "KMD 2027 golden purchase credit note",
                "account_id": expense_account_id, "tax_code_id": tax_code_id,
                "quantity": Decimal("1"), "unit_price": net,
            }],
        )
        await supplier_credit_notes_svc.post_supplier_credit_note(session, scn.id, posted_by="pytest-kmd-2027")


async def _seed_20_example_ledger(company_id, by_code, tax) -> None:
    income, expense = by_code["4-1000"], by_code["5-1000"]
    C = lambda name, ctype, reg: _contact(company_id, name, ctype, reg)  # noqa: E731

    # -- Sales (invoices) --
    await _post_invoice(company_id, await C("Arve esitaja OÜ", ContactType.CUSTOMER, "11111111"), income, tax["standard"], net=_D("2400.00"), issue_date=date(2027, 1, 1))           # row 1  M_101
    # row 2 — M_101 prepayment. A Payment received BEFORE the invoice's own
    # issue_date is the field-swap signal (generator.py docstring); the row
    # reports the SAME (M_101, 2500, 0.24) triple, with documentApplyToNumber
    # standing in for documentNumber.
    await _post_prepaid_invoice(
        company_id, await C("Ettemaks OÜ", ContactType.CUSTOMER, "11111114"), income, tax["standard"], by_code["1-1110"],
        net=_D("2500.00"), invoice_issue_date=date(2027, 1, 20), payment_date=date(2027, 1, 2),
    )
    await _post_invoice(company_id, await C("EE Pöördmaks OÜ", ContactType.CUSTOMER, "11111119"), income, tax["rc_domestic_supply"], net=_D("1700.00"), issue_date=date(2027, 1, 8))  # row 5  M_105
    await _post_invoice(company_id, await C("Natural Person Buyer", ContactType.CUSTOMER, None), income, tax["standard"], net=_D("1000.00"), issue_date=date(2027, 1, 3))            # row 6  M_101 cat200
    await _post_invoice(company_id, await C("Alla 1000 OÜ", ContactType.CUSTOMER, "11111113"), income, tax["standard"], net=_D("175.00"), issue_date=date(2027, 1, 2))               # row 7  M_101 (sample 103)
    await _post_invoice(company_id, await C("EU Kaup OY", ContactType.CUSTOMER, "FI08611111"), income, tax["zero_ic_goods"], net=_D("1200.00"), issue_date=date(2027, 1, 9))         # row 8  M_201
    await _post_invoice(company_id, await C("Export Customer", ContactType.CUSTOMER, None), income, tax["zero_export"], net=_D("950.00"), issue_date=date(2027, 1, 10))              # row 10 M_208
    await _post_invoice(company_id, await C("Exempt Customer", ContactType.CUSTOMER, None), income, tax["exempt"], net=_D("875.00"), issue_date=date(2027, 1, 11))                   # row 12 M_301
    # row 13 — M_101 credit note (signed −1000). Canonical CN helper does not
    # link an original invoice (the ALGSE_ARVE_KP boundary), so no original is posted.
    await _post_credit_note(company_id, await C("Mitu Kreeditid OÜ", ContactType.CUSTOMER, "11111131"), income, tax["standard"], net=_D("1000.00"), issue_date=date(2027, 1, 31))

    # -- Purchases (bills) --
    # rows 14 + 17 — rc_eu_acq_goods fan-out (S_101 base + O_401 input). Two bills
    # reproduce the sample's two independent amounts (S_101 2500, O_401 72).
    await _post_bill(company_id, await C("RC EU Goods Supplier (row 14)", ContactType.SUPPLIER, None), expense, tax["rc_eu_acq_goods"], net=_D("2500.00"), issue_date=date(2027, 1, 6))
    await _post_bill(company_id, await C("RC EU Goods Supplier (row 17)", ContactType.SUPPLIER, None), expense, tax["rc_eu_acq_goods"], net=_D("300.00"), issue_date=date(2027, 1, 7))
    await _post_bill(company_id, await C("Sisend OÜ", ContactType.SUPPLIER, "13000001"), expense, tax["standard"], net=_D("1000.00"), issue_date=date(2027, 1, 5))                    # row 15 O_101 = 240
    await _post_bill(company_id, await C("Customs Import Agent", ContactType.SUPPLIER, None), expense, tax["input_import"], net=_D("5000.00"), issue_date=date(2027, 1, 12))          # row 16 O_106 = 450 @9%
    await _post_bill(company_id, await C("Alla 1000 Sisend OÜ", ContactType.SUPPLIER, "11111125"), expense, tax["standard"], net=_D("300.00"), issue_date=date(2027, 1, 13))         # row 18 O_101 = 72 (sample 103)
    # row 19 — O_101 purchase-side credit note (signed −240), mirrors row 15's
    # bill through the SAME (reporting_type, role) resolution, sign=-1.
    await _post_supplier_credit_note(
        company_id, await C("Sisend Kreedit OÜ", ContactType.SUPPLIER, "14000001"), expense, tax["standard"],
        net=_D("1000.00"), issue_date=date(2027, 1, 29),
    )


def _find(rows, code: str, amount: Decimal):
    return next((r for r in rows if r.kmdtyyp_code == code and r.amount == amount), None)


async def test_kmd_2027_golden_reproduces_emta_sample() -> None:
    sample = _parse_sample_rows(_SAMPLE_PATH)
    assert len(sample) == 20, f"expected 20 example rows, found {len(sample)}"

    company_id, by_code, tax = await _company_with_all_codes()
    await _seed_20_example_ledger(company_id, by_code, tax)

    async with AsyncSessionLocal() as session:
        listing = await generate_kmd_2027(session, company_id=company_id, period_start=_PERIOD_START, period_end=_PERIOD_END)

    rows = listing.rows
    triage: dict[int, str] = {}

    # No transaction was dropped-with-flag: every posted line classified cleanly.
    assert not listing.errors, [e.message for e in listing.errors]

    # ---- bucket 1: produced examples — economic triple (code, amount, rate) ----
    reproduced = 0
    for no in _PRODUCED:
        s = sample[no]
        match = _find(rows, s["account_sub_id"], s["amount"])
        if match is None:
            triage[no] = f"FAIL-MISSING {s['account_sub_id']} {s['amount']}"
            continue
        if s["rate"] is not None and match.tax_rate != s["rate"]:
            triage[no] = f"FAIL-RATE {match.tax_rate} != {s['rate']}"
            continue
        if s["rate"] is None and match.tax_rate is not None:
            triage[no] = f"FAIL-RATE expected no taxes, got {match.tax_rate}"
            continue
        reproduced += 1
        triage[no] = "REPRODUCED"

    # ---- bucket 2: no-mapping codes must never appear ----
    produced_codes = {r.kmdtyyp_code for r in rows}
    for code in _NO_MAPPING_CODES:
        assert code not in produced_codes, f"UNMAPPED code {code} was produced"

    # ---- row 2: prepayment field-swap — documentApplyToNumber stands in for
    # documentNumber on this row (and ONLY this row); everything else the same
    # M_101 sale row every other invoice gets.
    r2 = _find(rows, "M_101", _D("2500.00"))
    assert r2 is not None and r2.tax_rate == _D("0.24")
    assert r2.document_number is None and r2.document_apply_to_number is not None
    prepay_rows = [r for r in rows if r.document_apply_to_number]
    assert len(prepay_rows) == 1 and prepay_rows[0] is r2

    # ---- row 19: purchase-side credit note — SAME O_101 leaf as row 15's
    # bill, signed negative (mirrors the sale side's credit-note signing).
    r19 = _find(rows, "O_101", _D("-240.00"))
    assert r19 is not None and r19.tax_rate == _D("0.24")

    # ---- documented canonical divergences (RE-DERIVED; not the donor's values) ----
    # Row 7 / 18: canonical emits category 100 + the real partner code, NOT the
    # sample's optional 103 omission.
    r7 = _find(rows, "M_101", _D("175.00"))
    assert r7 is not None and r7.identifier_category == m.IDENT_CAT_STANDARD and r7.partner_code == "11111113"
    r18 = _find(rows, "O_101", _D("72.00"))
    assert r18 is not None and r18.identifier_category == m.IDENT_CAT_STANDARD and r18.partner_code == "11111125"
    # Row 8: KMKR partner matches; country subaccount is the omitted boundary.
    r8 = _find(rows, "M_201", _D("1200.00"))
    assert r8 is not None
    assert r8.partner_code == "FI08611111" and r8.partner_code_type == m.IDENT_DESC_VAT_NUMBER
    assert r8.country_code is None  # boundary: RTK2T2013ap country accountSub not produced
    # Row 13: credit-note original-invoice date is the omitted boundary; ARVE = −1000.
    r13 = _find(rows, "M_101", _D("-1000.00"))
    assert r13 is not None and r13.original_invoice_dates == () and r13.invoice_total == _D("-1000.00")
    # Row 1 (sale): ARVE_KOGUSUMMA emitted = ex-VAT subtotal (superset over sample-omitting rows).
    r1 = _find(rows, "M_101", _D("2400.00"))
    assert r1 is not None and r1.invoice_total == _D("2400.00")
    # Row 15 (purchase): bill-side ARVE is the omitted boundary.
    r15 = _find(rows, "O_101", _D("240.00"))
    assert r15 is not None and r15.invoice_total is None

    # ---- fan-out completeness (rows 14/17 + two documented "other half" extras) ----
    s101 = sorted(r.amount for r in rows if r.kmdtyyp_code == "S_101")
    o401 = sorted(r.amount for r in rows if r.kmdtyyp_code == "O_401")
    assert s101 == [_D("300.00"), _D("2500.00")], s101
    assert o401 == [_D("72.00"), _D("600.00")], o401

    # ---- total row correspondence is complete: 15 produced + 2 fan-out extras ----
    assert len(rows) == len(_PRODUCED) + 2, (
        f"expected {len(_PRODUCED) + 2} rows, got {len(rows)}: "
        f"{sorted((r.kmdtyyp_code, str(r.amount)) for r in rows)}"
    )

    # ---- HEADLINE: all 15 produced examples reproduced on the economic triple ----
    print("KMD2027 golden triage:", {n: triage.get(n, "?") for n in _PRODUCED})
    assert reproduced == len(_PRODUCED), f"only {reproduced}/{len(_PRODUCED)} reproduced: {triage}"

    # ---- serialize the reconstructed real-ledger period + validate against the real XSD ----
    ctx = Kmd2027ReportingContext(
        regcode="10001234", period_start=_PERIOD_START, period_end=_PERIOD_END,
        creator_name="KMD2027 Golden Test OÜ",
    )
    xml = build_kmd_2027_xml_document(listing, ctx)
    errors = validate_against_xsd(xml)
    assert errors == [], errors

    # Re-pin the exported golden — SAEBOOKS_REGEN_FIXTURES=1, same convention
    # as the other lodgement golden tests' ``_maybe_regen`` helpers, writing
    # the REAL DB-driven+serialized bytes (not a hand-transcribed copy) to the
    # path SAEBOOKS_APA_GOLDEN_OUT names (default: a mount point a caller
    # bind-mounts in — this repo checkout has no path to
    # ``~/records/saebooks`` outside the container, so the write is a no-op
    # unless that directory exists).
    if os.environ.get("SAEBOOKS_REGEN_FIXTURES"):
        out_path = Path(os.environ.get("SAEBOOKS_APA_GOLDEN_OUT", "/records-out/kmd-apa-2027-golden.xml"))
        if out_path.parent.is_dir():
            out_path.write_bytes(xml)
