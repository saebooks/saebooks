"""``services.einvoice.generator`` — postgres_only golden test.

Näidis-OÜ-shaped: reuses ``_make_ee_company`` (same cross-module import
pattern ``test_kmd_inf_generator.py``/``test_kmd_2027_apa_golden.py`` already
establish), posts a REAL invoice through ``services.invoices`` (not a raw
journal entry — the generator reads the ``Invoice``/``InvoiceLine`` tables),
generates the e-invoice, and validates the output against the REAL UBL 2.1
XSD plus value cross-checks against the posted invoice's own totals.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest
from lxml import etree
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from saebooks.api.v1.auth import DEFAULT_TENANT_ID
from saebooks.db import AsyncSessionLocal
from saebooks.models.account import Account, AccountType
from saebooks.models.company import Company
from saebooks.models.contact import Contact, ContactType
from saebooks.models.invoice import Invoice
from saebooks.models.tax_code import TaxCode
from saebooks.services import business_identifiers
from saebooks.services import invoices as invoices_svc
from saebooks.services import payments as payments_svc
from saebooks.services.einvoice import mapping as m
from saebooks.services.einvoice.generator import (
    EInvoiceDataError,
    EInvoiceMappingError,
    EInvoiceStatusError,
    SellerIdentity,
    generate_einvoice,
)
from tests.services.einvoice._ubl_validation import validate_ubl_invoice
from tests.services.test_tax_return_generator import _make_ee_company

pytestmark = pytest.mark.postgres_only

_NS = {"cac": m.NS_CAC, "cbc": m.NS_CBC}


async def _naidis_company() -> uuid.UUID:
    company_id = await _make_ee_company(jurisdiction="EE")
    async with AsyncSessionLocal() as session:
        company = await session.get(Company, company_id)
        company.name = "Näidis OÜ"
        company.legal_name = "Näidis OÜ"
        # The Estonian äriregistri kood, recorded under its own ``ee_regcode``
        # business identifier (the legacy overloaded ``companies.abn`` column
        # was dropped in 0204). einvoice resolves it via
        # ``business_identifiers.primary_registry_identifier`` for an EE seller.
        await business_identifiers.upsert(
            session, company.id, "ee_regcode", "10137025",
            tenant_id=company.tenant_id,
        )
        # _make_ee_company's own accounts dict (test_tax_return_generator.py)
        # seeds bank/income/expense/fixed_asset/gst_* but no AR control
        # account — services.invoices.post_invoice needs the EE-convention
        # 1200 Ostjatega arveldused (Trade Debtors) that the jurisdiction-
        # aware control_accounts resolver returns for a jurisdiction="EE"
        # company (same gap test_kmd_2027_apa_golden.py's own
        # _company_with_all_codes helper fills for the same reason).
        session.add(Account(
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="1200",
            name="Trade Debtors", account_type=AccountType.ASSET,
        ))
        await session.commit()
    return company_id


async def _ostja_contact(company_id: uuid.UUID) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        c = Contact(
            company_id=company_id, name="Ostja AS", contact_type=ContactType.CUSTOMER,
            registration_number="12345678", address_line1="Ostja tee 2",
            city="Tartu", postcode="50001", country="Estonia",
        )
        session.add(c)
        await session.commit()
        return c.id


async def _account_id(company_id: uuid.UUID, code: str) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(Account.id).where(Account.company_id == company_id, Account.code == code)
            )
        ).scalar_one()


async def _tax_code_id(company_id: uuid.UUID, reporting_type: str) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(TaxCode.id).where(
                    TaxCode.company_id == company_id, TaxCode.reporting_type == reporting_type
                )
            )
        ).scalars().first()


async def _add_tax_code(company_id: uuid.UUID, reporting_type: str, *, rate: str = "0.000") -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        tc = TaxCode(
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID,
            code=f"T-{reporting_type[:12].upper()}", name=f"test code — {reporting_type}",
            rate=Decimal(rate), tax_system="VAT", jurisdiction="EE", reporting_type=reporting_type,
        )
        session.add(tc)
        await session.commit()
        return tc.id


async def _post_standard_invoice(company_id: uuid.UUID, contact_id: uuid.UUID, *, currency: str = "EUR") -> uuid.UUID:
    income = await _account_id(company_id, "4-1000")
    tax_code_id = await _tax_code_id(company_id, "standard")
    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25),
            currency=currency,
            lines=[{
                "description": "Konsultatsioon", "account_id": income,
                "tax_code_id": tax_code_id, "quantity": Decimal("10"), "unit_price": Decimal("100.00"),
            }],
        )
        await invoices_svc.post_invoice(session, inv.id, posted_by="pytest-einvoice-golden")
        return inv.id


async def test_golden_standard_rate_invoice_produces_valid_einvoice() -> None:
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    invoice_id = await _post_standard_invoice(company_id, contact_id)

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(
            session, invoice_id,
            seller=SellerIdentity(
                vat_number="EE101370251", street_name="Näidise tn 1",
                city_name="Tallinn", postal_zone="10111",
            ),
        )

    validate_ubl_invoice(xml_bytes)
    root = etree.fromstring(xml_bytes)

    assert root.findtext("cbc:InvoiceTypeCode", namespaces=_NS) == "380"
    assert root.findtext("cbc:DocumentCurrencyCode", namespaces=_NS) == "EUR"

    supplier_name = root.findtext(
        "cac:AccountingSupplierParty/cac:Party/cac:PartyName/cbc:Name", namespaces=_NS
    )
    assert supplier_name == "Näidis OÜ"
    supplier_regcode = root.findtext(
        "cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:CompanyID", namespaces=_NS
    )
    assert supplier_regcode == "10137025"
    supplier_endpoint = root.find(
        "cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", namespaces=_NS
    )
    assert supplier_endpoint.get("schemeID") == m.EAS_EE_REGISTRIKOOD

    buyer_name = root.findtext(
        "cac:AccountingCustomerParty/cac:Party/cac:PartyName/cbc:Name", namespaces=_NS
    )
    assert buyer_name == "Ostja AS"
    buyer_country = root.findtext(
        "cac:AccountingCustomerParty/cac:Party/cac:PostalAddress/cac:Country/cbc:IdentificationCode",
        namespaces=_NS,
    )
    assert buyer_country == "EE"  # derived from Contact.country="Estonia"

    payable = Decimal(root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=_NS))
    assert payable == Decimal("1240.00")  # 1000 net + 24% VAT
    tax_category = root.findtext(
        "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID", namespaces=_NS
    )
    assert tax_category == "S"
    tax_percent = root.findtext(
        "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:Percent", namespaces=_NS
    )
    assert Decimal(tax_percent) == Decimal("24.00")


async def test_generate_refuses_draft_invoice() -> None:
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    income = await _account_id(company_id, "4-1000")
    tax_code_id = await _tax_code_id(company_id, "standard")

    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25), currency="EUR",
            lines=[{
                "description": "Draft only", "account_id": income,
                "tax_code_id": tax_code_id, "quantity": Decimal("1"), "unit_price": Decimal("100.00"),
            }],
        )
        invoice_id = inv.id

    async with AsyncSessionLocal() as session:
        with pytest.raises(EInvoiceStatusError, match="not POSTED"):
            await generate_einvoice(session, invoice_id)


async def test_generate_refuses_non_eur_currency() -> None:
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    invoice_id = await _post_standard_invoice(company_id, contact_id, currency="AUD")

    async with AsyncSessionLocal() as session:
        with pytest.raises(EInvoiceStatusError, match="EUR"):
            await generate_einvoice(session, invoice_id)


async def test_generate_raises_mapping_error_for_purchase_only_reporting_type() -> None:
    """A line whose TaxCode.reporting_type is a purchase-side-only tag
    reaching the sale-side mapping is a data-integrity bug upstream, not
    something to silently paper over — see mapping.py's SCOPE section.
    Uses ``ic_acq_exempt`` (a real purchase-only informative tag per
    tax_codes.yaml's own header) posted, unusually, on an AR invoice line —
    the engine's tax computation has no side-restriction (it is
    ``services.einvoice``'s SCOPE that draws this line, not the ledger's),
    so the invoice posts fine and the failure surfaces exactly where this
    packet's scope says it must: at e-invoice generation time."""
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    income = await _account_id(company_id, "4-1000")
    bogus_tax_code_id = await _add_tax_code(company_id, "ic_acq_exempt")

    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25), currency="EUR",
            lines=[{
                "description": "Mistagged purchase-side code on a sale",
                "account_id": income, "tax_code_id": bogus_tax_code_id,
                "quantity": Decimal("1"), "unit_price": Decimal("100.00"),
            }],
        )
        await invoices_svc.post_invoice(session, inv.id, posted_by="pytest-einvoice-golden")
        invoice_id = inv.id

    async with AsyncSessionLocal() as session:
        with pytest.raises(EInvoiceMappingError):
            await generate_einvoice(session, invoice_id)


async def test_generate_requires_explicit_buyer_country_override_for_unresolvable_contact_country() -> None:
    """Contact.country is free text; a value with no ISO-2 mapping must
    raise loudly rather than emit a fabricated BT-55 country code — then
    succeed once the caller supplies the override."""
    company_id = await _naidis_company()
    async with AsyncSessionLocal() as session:
        c = Contact(
            company_id=company_id, name="Unmappable Country Buyer",
            contact_type=ContactType.CUSTOMER, registration_number="99999999",
            country="Ruritania",
        )
        session.add(c)
        await session.commit()
        contact_id = c.id

    invoice_id = await _post_standard_invoice(company_id, contact_id)

    async with AsyncSessionLocal() as session:
        with pytest.raises(EInvoiceDataError, match="country code"):
            await generate_einvoice(session, invoice_id)

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(
            session, invoice_id, buyer_country_code="XX",
            seller=SellerIdentity(vat_number="EE101370251"),
        )
    validate_ubl_invoice(xml_bytes)


async def test_multiline_same_category_tax_amount_rounds_once_at_group_level() -> None:
    """Critic round 4 finding: five same-category lines each net €0.02 @
    24% independently round to €0.00 per line (0.02 x 0.24 = 0.0048), but
    summing those per-line figures for TaxSubtotal/TaxAmount is BR-CO-17
    wrong — the category-level figure is round(0.10 x 0.24, 2) = €0.02.
    Also cross-checks BT-110 == sum(BT-117) (BR-CO-14) and
    TaxExclusive + Tax == TaxInclusive (BR-CO-15)."""
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    income = await _account_id(company_id, "4-1000")
    tax_code_id = await _tax_code_id(company_id, "standard")

    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25), currency="EUR",
            lines=[
                {
                    "description": f"Line {i}", "account_id": income,
                    "tax_code_id": tax_code_id, "quantity": Decimal("1"),
                    "unit_price": Decimal("0.02"),
                }
                for i in range(5)
            ],
        )
        await invoices_svc.post_invoice(session, inv.id, posted_by="pytest-einvoice-golden")
        invoice_id = inv.id

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(
            session, invoice_id, seller=SellerIdentity(vat_number="EE101370251"),
        )

    validate_ubl_invoice(xml_bytes)
    root = etree.fromstring(xml_bytes)

    header_tax = Decimal(root.findtext("cac:TaxTotal/cbc:TaxAmount", namespaces=_NS))
    subtotal_tax = Decimal(
        root.findtext("cac:TaxTotal/cac:TaxSubtotal/cbc:TaxAmount", namespaces=_NS)
    )
    assert header_tax == Decimal("0.02")
    assert subtotal_tax == Decimal("0.02")

    tax_exclusive = Decimal(
        root.findtext("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", namespaces=_NS)
    )
    tax_inclusive = Decimal(
        root.findtext("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=_NS)
    )
    payable = Decimal(root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=_NS))
    assert tax_exclusive == Decimal("0.10")
    assert tax_inclusive == tax_exclusive + header_tax == Decimal("0.12")
    assert payable == Decimal("0.12")


async def test_partial_payment_nets_out_payable_amount_and_emits_prepaid() -> None:
    """Critic round 4 finding: a POSTED invoice already partly settled by a
    posted payment must not e-invoice the buyer for the full original
    total — PayableAmount (BT-115) nets out inv.amount_paid per BR-CO-16,
    and PrepaidAmount (BT-113) surfaces the settled figure."""
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    invoice_id = await _post_standard_invoice(company_id, contact_id)  # total 1240.00
    bank_id = await _account_id(company_id, "1-1110")

    async with AsyncSessionLocal() as session:
        pay = await payments_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            bank_account_id=bank_id, payment_date=date(2026, 7, 15),
            amount=Decimal("500.00"), currency="EUR",
        )
    async with AsyncSessionLocal() as session:
        await payments_svc.post_payment(session, pay.id, posted_by="pytest-einvoice-golden")
    async with AsyncSessionLocal() as session:
        await payments_svc.allocate(
            session, pay.id, invoice_allocations=[(invoice_id, Decimal("500.00"))],
        )

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(
            session, invoice_id,
            seller=SellerIdentity(
                vat_number="EE101370251", street_name="Näidise tn 1",
                city_name="Tallinn", postal_zone="10111",
            ),
        )

    validate_ubl_invoice(xml_bytes)
    root = etree.fromstring(xml_bytes)

    tax_inclusive = Decimal(
        root.findtext("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", namespaces=_NS)
    )
    prepaid = Decimal(root.findtext("cac:LegalMonetaryTotal/cbc:PrepaidAmount", namespaces=_NS))
    payable = Decimal(root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=_NS))
    assert tax_inclusive == Decimal("1240.00")
    assert prepaid == Decimal("500.00")
    assert payable == Decimal("740.00")


async def test_reverse_charge_line_requires_buyer_vat_number() -> None:
    """Critic round 4 finding: an AE/K-category line needs the buyer's VAT
    identifier (BT-48) to substantiate the buyer's own VAT self-accounting
    — refuse to generate rather than silently omit it, then succeed once
    the caller supplies ``buyer_vat_number``."""
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    income = await _account_id(company_id, "4-1000")
    rc_tax_code_id = await _add_tax_code(company_id, "rc_domestic_supply")

    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25), currency="EUR",
            lines=[{
                "description": "Scrap metal (domestic reverse charge)",
                "account_id": income, "tax_code_id": rc_tax_code_id,
                "quantity": Decimal("1"), "unit_price": Decimal("100.00"),
            }],
        )
        await invoices_svc.post_invoice(session, inv.id, posted_by="pytest-einvoice-golden")
        invoice_id = inv.id

    async with AsyncSessionLocal() as session:
        with pytest.raises(EInvoiceDataError, match="Buyer VAT identifier"):
            await generate_einvoice(session, invoice_id)

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(
            session, invoice_id, buyer_vat_number="EE123456789",
        )
    validate_ubl_invoice(xml_bytes)
    root = etree.fromstring(xml_bytes)
    buyer_vat = root.findtext(
        "cac:AccountingCustomerParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
        namespaces=_NS,
    )
    assert buyer_vat == "EE123456789"
    tax_category = root.findtext(
        "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID", namespaces=_NS
    )
    assert tax_category == m.CAT_REVERSE_CHARGE


async def _post_invoice(
    company_id: uuid.UUID, contact_id: uuid.UUID, lines: list[dict], *, currency: str = "EUR"
) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        inv = await invoices_svc.create_draft(
            session, company_id=company_id, contact_id=contact_id,
            issue_date=date(2026, 7, 11), due_date=date(2026, 7, 25),
            currency=currency, lines=lines,
        )
        await invoices_svc.post_invoice(session, inv.id, posted_by="pytest-einvoice-golden")
        return inv.id


_SELLER = SellerIdentity(
    vat_number="EE101370251", street_name="Näidise tn 1",
    city_name="Tallinn", postal_zone="10111",
)


async def test_standard_rate_line_requires_seller_or_buyer_vat_id() -> None:
    """Verifier finding (BR-S-02, symmetric to the round-4 buyer-VAT guard):
    a Standard-rated (S) line must carry a Seller VAT identifier (BT-31) —
    or, failing that, a Buyer VAT identifier (BT-48) — refuse rather than
    emit a standard-rate e-invoice with no VAT identifier anywhere, then
    succeed once SellerIdentity.vat_number is supplied."""
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    invoice_id = await _post_standard_invoice(company_id, contact_id)

    async with AsyncSessionLocal() as session:
        with pytest.raises(EInvoiceDataError, match="Seller VAT identifier"):
            await generate_einvoice(session, invoice_id)  # default SellerIdentity, no VAT

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(session, invoice_id, seller=_SELLER)
    validate_ubl_invoice(xml_bytes)
    root = etree.fromstring(xml_bytes)
    seller_vat = root.findtext(
        "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID",
        namespaces=_NS,
    )
    assert seller_vat == "EE101370251"


async def test_line_discount_keeps_price_times_quantity_equal_to_line_total() -> None:
    """Finding #1 (round-3 fix, previously untested): with a line-level
    discount the UBL Price/PriceAmount (BT-146) must be the discounted item
    NET price, so InvoicedQuantity x PriceAmount == LineExtensionAmount
    (BT-131) holds without a separate cac:AllowanceCharge. quantity=10,
    unit_price=100.00, discount_pct=10 posts line_subtotal=900.00."""
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    income = await _account_id(company_id, "4-1000")
    tax_code_id = await _tax_code_id(company_id, "standard")
    invoice_id = await _post_invoice(company_id, contact_id, [{
        "description": "Konsultatsioon (10% allahindlus)", "account_id": income,
        "tax_code_id": tax_code_id, "quantity": Decimal("10"),
        "unit_price": Decimal("100.00"), "discount_pct": Decimal("10"),
    }])

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(session, invoice_id, seller=_SELLER)

    validate_ubl_invoice(xml_bytes)
    root = etree.fromstring(xml_bytes)
    line = root.find("cac:InvoiceLine", namespaces=_NS)
    qty = Decimal(line.findtext("cbc:InvoicedQuantity", namespaces=_NS))
    price = Decimal(line.findtext("cac:Price/cbc:PriceAmount", namespaces=_NS))
    line_ext = Decimal(line.findtext("cbc:LineExtensionAmount", namespaces=_NS))
    assert line_ext == Decimal("900.00")  # discounted net, not gross 1000
    assert price == Decimal("90.00")      # discounted unit price, not gross 100
    assert qty * price == line_ext        # the BT-146/BT-131 tie-out


async def test_two_distinct_exempt_reasons_are_not_merged_into_one_subtotal() -> None:
    """Finding #2 (round-3 fix, previously untested): two exempt (category E)
    lines with different KMS §16 exemption sub-reasons must NOT be merged
    into a single cac:TaxSubtotal that keeps only the first line's
    TaxExemptionReason — the grouping key includes the reason text, so each
    keeps its own reason and taxable amount."""
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    income = await _account_id(company_id, "4-1000")
    async with AsyncSessionLocal() as session:
        tc_fin = TaxCode(
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="EXEMPT-FIN",
            name="Finantsteenus (KMS §16 lg 2 p 6)", rate=Decimal("0.000"),
            tax_system="VAT", jurisdiction="EE", reporting_type="exempt",
        )
        tc_re = TaxCode(
            company_id=company_id, tenant_id=DEFAULT_TENANT_ID, code="EXEMPT-RE",
            name="Kinnisvara üür (KMS §16 lg 2 p 2)", rate=Decimal("0.000"),
            tax_system="VAT", jurisdiction="EE", reporting_type="exempt",
        )
        session.add_all([tc_fin, tc_re])
        await session.commit()
        fin_id, re_id = tc_fin.id, tc_re.id

    invoice_id = await _post_invoice(company_id, contact_id, [
        {"description": "Laenu intress", "account_id": income, "tax_code_id": fin_id,
         "quantity": Decimal("1"), "unit_price": Decimal("300.00")},
        {"description": "Büroopinna üür", "account_id": income, "tax_code_id": re_id,
         "quantity": Decimal("1"), "unit_price": Decimal("500.00")},
    ])

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(session, invoice_id)  # E-only: no VAT id required

    validate_ubl_invoice(xml_bytes)
    root = etree.fromstring(xml_bytes)
    subtotals = root.findall("cac:TaxTotal/cac:TaxSubtotal", namespaces=_NS)
    assert len(subtotals) == 2  # NOT merged into one
    reasons = {
        s.findtext("cac:TaxCategory/cbc:TaxExemptionReason", namespaces=_NS) for s in subtotals
    }
    assert reasons == {
        "Finantsteenus (KMS §16 lg 2 p 6)", "Kinnisvara üür (KMS §16 lg 2 p 2)",
    }
    taxables = {
        Decimal(s.findtext("cbc:TaxableAmount", namespaces=_NS)) for s in subtotals
    }
    assert taxables == {Decimal("300.00"), Decimal("500.00")}


async def test_missing_buyer_registrikood_degrades_without_error() -> None:
    """Finding #3 (documented won't-fix, previously untested): a buyer with
    no registration_number and no peppol_participant_id must still generate a
    valid document (graceful degradation — the routing address is the
    operator adapter's concern), emitting NO buyer EndpointID and NO
    PartyIdentification/PartyLegalEntity CompanyID, and raising no error."""
    company_id = await _naidis_company()
    async with AsyncSessionLocal() as session:
        c = Contact(
            company_id=company_id, name="Registrikoodita Ostja",
            contact_type=ContactType.CUSTOMER, registration_number=None,
            country="Estonia",
        )
        session.add(c)
        await session.commit()
        contact_id = c.id
    invoice_id = await _post_standard_invoice(company_id, contact_id)

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(session, invoice_id, seller=_SELLER)

    validate_ubl_invoice(xml_bytes)
    root = etree.fromstring(xml_bytes)
    buyer = root.find("cac:AccountingCustomerParty/cac:Party", namespaces=_NS)
    assert buyer.find("cbc:EndpointID", namespaces=_NS) is None
    assert buyer.find("cac:PartyIdentification", namespaces=_NS) is None
    assert buyer.find("cac:PartyLegalEntity/cbc:CompanyID", namespaces=_NS) is None


async def test_buyer_peppol_participant_id_drives_endpoint_over_registrikood() -> None:
    """Finding #4 (round-3 fix, previously untested): when Contact.
    peppol_participant_id is captured it, NOT a fabricated registrikood
    EndpointID, drives cbc:EndpointID — and its scheme (here EAS_EE_VAT
    "9931") must NOT leak onto cac:PartyIdentification/cbc:ID, which carries
    the registrikood unscoped (finding #8 round-3 fix, verified here too)."""
    company_id = await _naidis_company()
    async with AsyncSessionLocal() as session:
        c = Contact(
            company_id=company_id, name="Peppol Ostja AS",
            contact_type=ContactType.CUSTOMER, registration_number="12345678",
            city="Tartu", postcode="50001", country="Estonia",
            peppol_participant_id=f"{m.EAS_EE_VAT}:EE101234567",
        )
        session.add(c)
        await session.commit()
        contact_id = c.id
    invoice_id = await _post_standard_invoice(company_id, contact_id)

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(session, invoice_id, seller=_SELLER)

    validate_ubl_invoice(xml_bytes)
    root = etree.fromstring(xml_bytes)
    endpoint = root.find(
        "cac:AccountingCustomerParty/cac:Party/cbc:EndpointID", namespaces=_NS
    )
    assert endpoint.get("schemeID") == m.EAS_EE_VAT  # "9931", from peppol_participant_id
    assert endpoint.text == "EE101234567"            # NOT the registrikood 12345678
    ident = root.find(
        "cac:AccountingCustomerParty/cac:Party/cac:PartyIdentification/cbc:ID", namespaces=_NS
    )
    assert ident.text == "12345678"        # registrikood
    assert ident.get("schemeID") is None   # 9931 must NOT leak onto it


async def test_non_terminating_unit_price_rendered_at_higher_precision() -> None:
    """Finding #5 (round-3 fix, previously untested): a re-derived unit price
    that doesn't terminate at 2dp (line_subtotal / quantity) is rendered by
    the dedicated 4dp _price_str formatter, NOT quantized to 2dp — so the
    quantity x PriceAmount drift stays within a cent of the authoritative
    LineExtensionAmount instead of the ~3 cents a 2dp Price would give.
    quantity=3, unit_price=33.35, discount_pct=1 posts a non-terminating
    99.05/3 quotient."""
    company_id = await _naidis_company()
    contact_id = await _ostja_contact(company_id)
    income = await _account_id(company_id, "4-1000")
    tax_code_id = await _tax_code_id(company_id, "standard")
    invoice_id = await _post_invoice(company_id, contact_id, [{
        "description": "Osaline ühik", "account_id": income,
        "tax_code_id": tax_code_id, "quantity": Decimal("3"),
        "unit_price": Decimal("33.35"), "discount_pct": Decimal("1"),
    }])

    # Pin assertions against the ACTUAL posted line_subtotal, not paper math.
    async with AsyncSessionLocal() as session:
        line = (await session.execute(
            select(Invoice).options(selectinload(Invoice.lines)).where(Invoice.id == invoice_id)
        )).scalar_one().lines[0]
        posted_subtotal = line.line_subtotal
        posted_qty = line.quantity

    async with AsyncSessionLocal() as session:
        xml_bytes = await generate_einvoice(session, invoice_id, seller=_SELLER)

    validate_ubl_invoice(xml_bytes)
    root = etree.fromstring(xml_bytes)
    line_el = root.find("cac:InvoiceLine", namespaces=_NS)
    price_text = line_el.findtext("cac:Price/cbc:PriceAmount", namespaces=_NS)
    line_ext = Decimal(line_el.findtext("cbc:LineExtensionAmount", namespaces=_NS))

    # LineExtensionAmount is the authoritative posted net.
    assert line_ext == posted_subtotal
    # Price is the 4dp-quantized quotient, NOT forced to 2dp.
    expected_price = (posted_subtotal / posted_qty).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    assert Decimal(price_text) == expected_price
    assert len(price_text.split(".")[1]) > 2  # more than 2dp => not the money formatter
    # quantity x higher-precision price reconstructs the line total within a cent.
    assert abs(posted_qty * Decimal(price_text) - line_ext) <= Decimal("0.01")
