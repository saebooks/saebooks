"""KMD-INF (VAT-return invoice annex) -> official e-MTA ``salesAnnex`` /
``purchasesAnnex`` element mapping.

CONFORMANCE STATUS — PINNED to the real e-MTA format (2026-07)
--------------------------------------------------------------
The former PLACEHOLDER names are replaced with the REAL element names from
``vatdeclaration.xsd`` + the KMD6 example
(``tests/fixtures/emta_schemas/vatdeclaration_example.xml``). KMD-INF is NOT a
standalone document: Part A = ``salesAnnex`` and Part B = ``purchasesAnnex``,
both children of the same ``vatDeclaration`` the KMD main form lives in. The
serializer therefore builds these annex ELEMENTS and hands them to
``kmd.serializer.build_vat_declaration_document`` (annex-only or combined).

Structural deltas from the old guessed model (see serializer.py):

* No ``JrkNr`` / row-number column, no ``KreeditArve`` boolean, no
  ``kmd_box_code`` column — the real ``saleLine`` has none of these. A credit
  note is just a ``saleLine`` with a NEGATIVE ``invoiceSum``.
* Part A row = ``saleLine`` with columns buyerRegCode, buyerName, invoiceNumber,
  invoiceDate, invoiceSum, taxRate, invoiceSumForRate, sumForRateInPeriod,
  comments (9). ``taxRate`` is the TAX_RATE_SALES classifier STRING ("24", "22",
  ...), not a decimal percentage.
* Part B row = ``purchaseLine`` with columns sellerRegCode, sellerName,
  invoiceNumber, invoiceDate, invoiceSumVat, vatSum, vatInPeriod, comments (8).
  There is NO rate column in Part B.
* Annex containers carry an optional ``groupMemberRegCode`` (VAT-group only,
  omitted for a plain taxpayer) then ``noSales``/``sumPerPartnerSales``
  (``noPurchases``/``sumPerPartnerPurchases`` for B) then the line rows.

⚠ UNVERIFIED (kept explicit):
  - ``invoiceSumForRate`` (cell 8, "cash-basis only") vs ``sumForRateInPeriod``
    (cell 9, period-declared taxable value): we emit our per-rate
    ``taxable_value`` into BOTH (equal for an accrual, single-period line).
  - ``vatSum`` (cell 7, "cash-basis only") vs ``vatInPeriod`` (cell 8,
    mandatory box-5 input VAT): we emit our ``input_vat`` into BOTH.
  - ``taxRate`` "erikord" (special-arrangement) variants (24erikord, ...) are
    NOT distinguished — the generator carries no special-arrangement flag on the
    rate; the plain numeric classifier is emitted.

erisus/COMMENT reconciliation (kmd_classifiers_01.07.2025.pdf): the generator's
derived erisus codes are ALL valid official classifier values — Part A "02"
(§41¹ buyer self-assess) and "03" (mixed-rate) ∈ COMMENT_SALES {01,02,03};
Part B "12" (§41¹ reverse-charge acquisition) ∈ COMMENT_PURCHASES {11,12}. The
generator deliberately never derives "01"/"11" (documented — no finer seed
leaf). No mapping table needed: the values pass straight through to ``comments``.
"""
from __future__ import annotations

from decimal import Decimal

# --- salesAnnex / purchasesAnnex element names -------------------------------
KMD_INF_EL_SALES_ANNEX = "salesAnnex"
KMD_INF_EL_PURCHASES_ANNEX = "purchasesAnnex"
KMD_INF_EL_GROUP_MEMBER_REGCODE = "groupMemberRegCode"
KMD_INF_EL_NO_SALES = "noSales"
KMD_INF_EL_NO_PURCHASES = "noPurchases"
KMD_INF_EL_SUM_PER_PARTNER_SALES = "sumPerPartnerSales"
KMD_INF_EL_SUM_PER_PARTNER_PURCHASES = "sumPerPartnerPurchases"
KMD_INF_EL_SALE_LINE = "saleLine"
KMD_INF_EL_PURCHASE_LINE = "purchaseLine"

# --- Part A (saleLine) columns: (generator-row attribute, element name) ------
# The row attribute is a KmdInfPartARow field EXCEPT the three synthesised
# columns (taxRate / invoiceSumForRate / sumForRateInPeriod) which the
# serializer derives — see serializer._sale_line_element. Order is the real
# SaleLine XSD/CSV order.
KMD_INF_PART_A_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("partner_registration_number", "buyerRegCode"),
    ("partner_name", "buyerName"),
    ("document_number", "invoiceNumber"),
    ("document_date", "invoiceDate"),
    ("document_total_ex_vat", "invoiceSum"),
    ("_tax_rate_classifier", "taxRate"),
    ("_invoice_sum_for_rate", "invoiceSumForRate"),
    ("_sum_for_rate_in_period", "sumForRateInPeriod"),
    ("erisuse_kood", "comments"),
)

# --- Part B (purchaseLine) columns -------------------------------------------
KMD_INF_PART_B_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("partner_registration_number", "sellerRegCode"),
    ("partner_name", "sellerName"),
    ("document_number", "invoiceNumber"),
    ("document_date", "invoiceDate"),
    ("document_total_incl_vat", "invoiceSumVat"),
    ("_vat_sum", "vatSum"),
    ("input_vat", "vatInPeriod"),
    ("erisuse_kood", "comments"),
)

# --- CSV ---------------------------------------------------------------------
# Same row-symbol CSV family as the KMD body: 'A' rows and 'B' rows, no
# column-name header, ';' delimiter, no trailing ';', CRLF, UTF-8.
KMD_INF_CSV_ENCODING = "utf-8"
KMD_INF_CSV_DELIMITER = ";"
KMD_INF_CSV_SALES_SYMBOL = "A"
KMD_INF_CSV_PURCHASES_SYMBOL = "B"


def tax_rate_classifier(rate: Decimal) -> str:
    """Map a decimal VAT rate (e.g. ``Decimal('24.00')``) to its TAX_RATE_SALES
    classifier code ("24"). Integer-valued rates only (all current EE rates are
    whole percents: 24/22/20/13/9/5). ``erikord`` variants are NOT distinguished
    (UNVERIFIED, see module docstring)."""
    q = rate.quantize(Decimal("1")) if rate == rate.to_integral_value() else rate
    return str(q.to_integral_value()) if rate == rate.to_integral_value() else str(q)
