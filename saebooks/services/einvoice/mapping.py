"""EN 16931 / Peppol BIS Billing 3.0 — UBL element names + code-list mapping.

Mirrors the ``lodgement/kmd*/mapping.py`` convention (pure constants + a
sourced-per-cell reporting_type -> code mapping, no DB/service imports).

SOURCES — see ``tests/fixtures/ubl21/SOURCES.md`` and
``tests/fixtures/peppol_bis3/SOURCES.md`` for the fetched artifacts this file
is built from. Summary:

* UBL 2.1 element names/namespaces: OASIS ``docs.oasis-open.org/ubl/os-UBL-2.1/
  xsd/`` (``UBL-Invoice-2.1.xsd`` + its ``common/`` imports).
* ``CustomizationID`` / ``ProfileID`` magic strings, the ``EndpointID``/
  ``PartyIdentification`` ISO 6523 scheme pattern, ``InvoiceTypeCode 380``:
  copied VERBATIM from ``tests/fixtures/peppol_bis3/examples/base-example.xml``
  (an official OpenPeppol BIS 3.0 example) — not hand-typed.
* BT-118 tax category code list: ``tests/fixtures/peppol_bis3/codelist/
  UNCL5305.xml`` — the OpenPeppol-curated EN 16931 subset of UN/EDIFACT
  UNCL5305: ``S / Z / E / AE / K / G / O / L / M`` (+ ``B``, Italy-only). There
  is **no ``AA`` code** in this list — a wrong assumption corrected here.
* BT-121 exemption reason code list: ``tests/fixtures/peppol_bis3/codelist/
  VATEX.xml``.
* ISO 6523 EAS scheme ids: ``tests/fixtures/peppol_bis3/codelist/eas.xml``,
  cross-checked against the independently-fetched Peppol eDelivery Network
  code list (same two Estonian rows, same fetch day — see that file's
  provenance note in SOURCES.md).

SCOPE — outbound sales invoices only (BT/BG scope of an EN 16931 Invoice,
UBL ``InvoiceTypeCode`` 380). A posted ``saebooks`` AR ``Invoice`` only ever
carries SALE-side ``TaxCode.reporting_type`` values (see
``saebooks/services/tax_engine/ee.py`` and ``saebooks/seeds/jurisdictions/EE/
tax_codes.yaml``) — the purchase-side reverse-charge fan-out tags
(``rc_eu_acq_goods`` / ``rc_eu_acq_services``) and the purchase-only
informative tags (``ic_acq_exempt`` / ``rc_domestic_acq`` / ``ee_acq_foreign``
/ ``input_import`` / ``input_car_*``) can never appear on an invoice line and
are deliberately NOT in ``REPORTING_TYPE_TO_TAX_CATEGORY`` below — a line
carrying one of them is a data-integrity bug upstream (a purchase-side code on
a sale), not an e-invoicing mapping gap, and the generator raises loudly
rather than silently mapping garbage (see ``generator.py``).

Credit notes (UBL ``CreditNote``, ``InvoiceTypeCode``/document family 381) are
OUT OF SCOPE for this packet — only posted ``Invoice`` rows (document type
380) are producible. ``CREDIT_NOTE_TYPE_CODE`` is kept as a documented
constant for a follow-up, not wired to any builder here.
"""
from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# UBL 2.1 namespaces (Invoice-2.1.xsd's own targetNamespace + its cac/cbc
# imports) — verbatim from tests/fixtures/ubl21/xsd/maindoc/UBL-Invoice-2.1.xsd.
# --------------------------------------------------------------------------- #
NS_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

NSMAP: dict[str, str] = {None: NS_INVOICE, "cac": NS_CAC, "cbc": NS_CBC}

# --------------------------------------------------------------------------- #
# BIS Billing 3.0 envelope magic strings — copied verbatim from
# tests/fixtures/peppol_bis3/examples/base-example.xml. One wrong character
# here fails every downstream validator, so these are NOT hand-typed from the
# spec prose — they are lifted from the publisher's own instance.
# --------------------------------------------------------------------------- #
CUSTOMIZATION_ID = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
PROFILE_ID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
INVOICE_TYPE_CODE = "380"  # Commercial invoice (UNCL1001 subset)
CREDIT_NOTE_TYPE_CODE = "381"  # NOT wired — credit notes are out of scope, see module docstring.

TAX_SCHEME_ID = "VAT"

# --------------------------------------------------------------------------- #
# ISO 6523 Electronic Address Scheme (EAS) — used for cbc:EndpointID/@schemeID
# and cac:PartyIdentification/cbc:ID/@schemeID. Source:
# tests/fixtures/peppol_bis3/codelist/eas.xml.
# --------------------------------------------------------------------------- #
EAS_EE_REGISTRIKOOD = "0191"  # Estonian äriregistri kood (Centre of Registers and Information Systems)
EAS_EE_VAT = "9931"  # Estonia VAT number

# UN/ECE Recommendation 20 unit code — default when the engine has no
# per-line unit-of-measure field (InvoiceLine carries none today). "C62"
# ("one" / piece) is BIS 3.0's own default choice in its minimal S-category
# worked example (Vat-category-S.xml) — see that file's SOURCES.md entry.
DEFAULT_UNIT_CODE = "C62"

# --------------------------------------------------------------------------- #
# BT-118 cac:TaxCategory/cbc:ID — UNCL5305 subset (codelist/UNCL5305.xml).
# --------------------------------------------------------------------------- #
CAT_STANDARD = "S"
CAT_ZERO_GOODS = "Z"
CAT_EXEMPT = "E"
CAT_REVERSE_CHARGE = "AE"
CAT_INTRA_COMMUNITY = "K"
CAT_EXPORT = "G"
CAT_OUTSIDE_SCOPE = "O"

# --------------------------------------------------------------------------- #
# BT-121 cbc:TaxExemptionReasonCode — VATEX subset (codelist/VATEX.xml).
# --------------------------------------------------------------------------- #
VATEX_REVERSE_CHARGE = "VATEX-EU-AE"
VATEX_INTRA_COMMUNITY_SUPPLY = "VATEX-EU-IC"
VATEX_EXPORT = "VATEX-EU-G"
VATEX_NOT_SUBJECT_TO_VAT = "VATEX-EU-O"


@dataclass(frozen=True, slots=True)
class TaxCategoryMapping:
    """One ``TaxCode.reporting_type`` -> BT-118/BT-119/BT-120/BT-121 cell.

    ``rate_carries_percent`` — True for every positive-rate category (S):
    ``cbc:Percent`` differentiates 24%/13%/9%/capital, NOT a distinct
    UNCL5305 code (all four are "S" — see ``UNCL5305.xml``'s own "Standard
    rate" description, singular). False for every zero/exempt/out-of-scope
    category, where ``cbc:Percent`` is conventionally omitted or 0 and the
    exemption reason (code and/or free text) is what a reader actually needs
    (BR-E-10/BR-AE-10/BR-Z-*/BR-G-10/BR-O-10 style rules — an EN 16931-valid
    zero-tax line always carries a reason, even though no UBL XSD enforces it).

    ``exemption_reason_code`` — a VATEX code where the mapping is
    unambiguous (K/G/AE/O below); ``None`` where it is genuinely ambiguous
    (see ``exempt`` below) — the generator falls back to the company's own
    ``TaxCode.name`` as BT-120 free text in that case, which is documented,
    not silently blank.

    ``sourcing`` — one line: which fixture/example this cell is pinned
    against, or "INFERRED" + why, per the SPEC-VS-INFERRED convention
    ``kmd_2027``/``ee_kmd3.py`` already use.
    """

    tax_category_id: str
    rate_carries_percent: bool
    exemption_reason_code: str | None
    sourcing: str


# Sale-side TaxCode.reporting_type values reachable from a posted AR Invoice
# line (see module docstring's SCOPE section). Every string here is a real
# reporting_type seeded/used somewhere in saebooks/seeds/jurisdictions/EE/
# tax_codes.yaml or tax_return_box_definitions.yaml's sale-side
# feeder_tax_codes lists — cross-checked box by box, not guessed.
REPORTING_TYPE_TO_TAX_CATEGORY: dict[str, TaxCategoryMapping] = {
    # --- Positive rates: all four are "S", differentiated only by Percent ---
    "standard": TaxCategoryMapping(
        CAT_STANDARD, True, None,
        "UNCL5305.xml 'Standard rate' + base-example.xml (S, Percent=25.0)",
    ),
    "standard_legacy_20": TaxCategoryMapping(
        CAT_STANDARD, True, None, "Same cell as 'standard' — historic 20% rate, current 24% rule",
    ),
    "standard_legacy_22": TaxCategoryMapping(
        CAT_STANDARD, True, None, "Same cell as 'standard' — historic 22% rate, current 24% rule",
    ),
    "reduced_9": TaxCategoryMapping(
        CAT_STANDARD, True, None, "Same cell as 'standard' — reduced rate is a Percent, not a category",
    ),
    "reduced_13": TaxCategoryMapping(
        CAT_STANDARD, True, None, "Same cell as 'standard'",
    ),
    "reduced_5_legacy": TaxCategoryMapping(
        CAT_STANDARD, True, None, "Same cell as 'standard' — historic 5% press rate",
    ),
    "capital": TaxCategoryMapping(
        CAT_STANDARD, True, None,
        "Same cell as 'standard' — capital-asset sale at the standard rate, still category S",
    ),
    # --- Exempt (KMS §16) ----------------------------------------------------
    # KMS §16 exempt supplies span several EU Directive 2006/112/EC articles
    # (financial/insurance services -> Art 135; healthcare/education/postal
    # -> Art 132; real estate -> Art 135) with no single VATEX code covering
    # all of them and no per-TaxCode article classification in this engine
    # (see tax_codes.yaml's own "⚠ CRITIC-ROUND-3 FINDING" — reporting_type
    # alone cannot discriminate). exemption_reason_code is deliberately None;
    # the generator uses the TaxCode's own `name` as BT-120 free text instead
    # (EN 16931 accepts BT-120 text OR BT-121 code, not both required —
    # vat-category-O.xml's own example uses text-only, no code).
    "exempt": TaxCategoryMapping(
        CAT_EXEMPT, False, None,
        "UNCL5305.xml 'Exempt from Tax' + vat-category-E.xml structure; "
        "reason INFERRED per-TaxCode (KMS §16 spans multiple EU articles)",
    ),
    # --- Zero-rated: intra-Community goods -----------------------------------
    "zero_ic_goods": TaxCategoryMapping(
        CAT_INTRA_COMMUNITY, False, VATEX_INTRA_COMMUNITY_SUPPLY,
        "UNCL5305.xml 'K = VAT exempt for EEA intra-community supply' + "
        "VATEX.xml 'VATEX-EU-IC = Intra-Community supply' (Art 138 goods)",
    ),
    # --- Zero-rated: intra-Community/Art-44 services -------------------------
    # INFERRED, flagged per the advisor review of this packet: KMS's
    # "teenuste käive teise liikmesriigi maksukohustuslasele" (services to a
    # taxable person in another member state, general B2B place-of-supply
    # rule) is the domestic analogue of EU Directive Art 44 — the RECIPIENT
    # self-accounts VAT in their own country, which is the textbook
    # "AE = VAT Reverse Charge" mechanism, so this cell is coded AE. NOTE
    # (critic round 3): UNCL5305.xml's own Id=K Name text ("VAT exempt for
    # EEA intra-community supply of goods AND services") is broader than
    # goods-only, so "K's usage is goods-only" is NOT a safe reading of that
    # codelist — the AE-over-K call here rests solely on the reverse-charge
    # mechanism (self-accounting shifts the VAT obligation to the recipient,
    # which is what AE denotes; K denotes a zero-rated supply with no such
    # shift), not on any goods/services split in the codelist label. No
    # worked BIS 3.0 example distinguishes this cell (see
    # peppol_bis3/SOURCES.md) and no EMTA/Peppol source confirms AE over K
    # for this specific KMS clause — treated as AE (reverse-charge
    # semantics) until a real Estonian e-invoice sample or EMTA guidance
    # settles it either way.
    "zero_ic_services": TaxCategoryMapping(
        CAT_REVERSE_CHARGE, False, VATEX_REVERSE_CHARGE,
        "INFERRED — Art 44 cross-border B2B services, reverse-charge semantics; "
        "no BIS 3.0 K-vs-AE worked example for this cell (see SOURCES.md)",
    ),
    # --- Zero-rated: export -----------------------------------------------
    "zero_export": TaxCategoryMapping(
        CAT_EXPORT, False, VATEX_EXPORT,
        "UNCL5305.xml 'G = Free export item' + VATEX.xml 'VATEX-EU-G = Export outside the EU'",
    ),
    "zero_traveller": TaxCategoryMapping(
        CAT_EXPORT, False, VATEX_EXPORT,
        "Same cell as 'zero_export' — tax_codes.yaml's own comment calls "
        "tax-free traveller sales 'a sub-case of export'",
    ),
    # --- Domestic reverse charge (KMS §41^1, seller side) --------------------
    "rc_domestic_supply": TaxCategoryMapping(
        CAT_REVERSE_CHARGE, False, VATEX_REVERSE_CHARGE,
        "UNCL5305.xml 'AE = Vat Reverse Charge' + VATEX.xml 'VATEX-EU-AE = Reverse charge' "
        "(KMS §41^1 real-estate/scrap-metal/precious-metal domestic RC, seller side)",
    ),
    # --- Installation/assembly goods installed in another member state ------
    # INFERRED: place of supply shifts to the installation member state, so
    # from Estonia's own VAT perspective the supply is not domestically
    # taxable — closest UNCL5305 fit is "O = Services outside scope of tax"
    # (no worked BIS 3.0 example for this specific KMD-box-9 scenario).
    "install_other_ms": TaxCategoryMapping(
        CAT_OUTSIDE_SCOPE, False, VATEX_NOT_SUBJECT_TO_VAT,
        "INFERRED — place of supply is the OTHER member state, not Estonia; "
        "closest UNCL5305 fit is 'O = Services outside scope of tax'",
    ),
    # --- Not reportable (outside VAT scope entirely) -------------------------
    "no_tax": TaxCategoryMapping(
        CAT_OUTSIDE_SCOPE, False, VATEX_NOT_SUBJECT_TO_VAT,
        "UNCL5305.xml 'O = Services outside scope of tax' + vat-category-O.xml + "
        "VATEX.xml 'VATEX-EU-O = Not subject to VAT'",
    ),
    "NTR": TaxCategoryMapping(
        CAT_OUTSIDE_SCOPE, False, VATEX_NOT_SUBJECT_TO_VAT,
        "Same cell as 'no_tax' — NTR is the RefTaxCode.code spelling of the same concept",
    ),
}


def resolve_tax_category(reporting_type: str) -> TaxCategoryMapping:
    """Look up ``reporting_type`` in the sale-side map. Raises ``KeyError``
    (via a clearer ``EInvoiceMappingError`` at the call site — see
    ``generator.py``) for anything not in ``REPORTING_TYPE_TO_TAX_CATEGORY``,
    rather than defaulting to a category that could misstate the VAT
    treatment on a legal document. A purchase-side tag (e.g.
    ``rc_eu_acq_goods``) reaching this function is itself the bug — see the
    module docstring's SCOPE section."""
    return REPORTING_TYPE_TO_TAX_CATEGORY[reporting_type]
