"""EE KMD (VAT return) box-code -> official e-MTA ``vatDeclaration`` element
mapping.

CONFORMANCE STATUS — PINNED to the real e-MTA format (2026-07)
--------------------------------------------------------------
The former PLACEHOLDER names have been replaced with the REAL element names
from e-MTA's ``vatdeclaration.xsd`` + the official KMD6 example
(``tests/fixtures/emta_schemas/vatdeclaration_example.xml``; full download set
at ``~/records/saebooks/emta-schemas/``). Sources: ``kmd2_description_of_xml_format.pdf``
(XML), ``kmd_description_of_csv_format_2025.pdf`` (CSV),
``kmd_classifiers_01.07.2025.pdf`` (TAX_RATE_SALES / COMMENT_SALES /
COMMENT_PURCHASES classifiers).

Structural facts (differ from the old guessed model — see serializer.py):

* Root is ``vatDeclaration`` in **no namespace** (the XSD declares
  ``elementFormDefault="qualified"`` but NO ``targetNamespace``). No prefix.
* The envelope carries ``taxPayerRegCode``, optional ``submitterPersonCode``,
  ``year``, ``month``, ``declarationType``, ``version`` — NOT a period
  start/end date pair. ``declarationBody`` (the KMD main form), ``salesAnnex``
  (KMD-INF Part A) and ``purchasesAnnex`` (KMD-INF Part B) are the three parts
  and MAY be transmitted separately (format PDF) — see
  ``serializer.build_vat_declaration_document``.
* ``declarationBody`` is NOT a flat 28-box vector. It carries four MANDATORY
  boolean flags then the monetary boxes (all ``minOccurs=0``). Four of our 28
  engine boxes — **4** (Käibemaks kokku), **4-1** (import VAT payable), **12**
  (tasumisele kuuluv), **13** (enammakstud) — are COMPUTED by e-MTA and have NO
  declarationBody element; they are dropped from the file (see
  ``KMD_COMPUTED_BOXES``).

⚠ UNVERIFIED items (kept explicit, not guessed):
  - Box **4-1** ("Impordilt tasumisele kuuluv käibemaks"): neither the KMD5 XSD
    nor the KMD6 example contains a line-4.1 element. Treated as computed /
    not-submitted. If a future KMD6 XSD exposes one, add it here.
  - ``selfSupply20`` / ``selfSupply9`` (pre-2014 legacy omatarve) and
    ``numberOfCars`` / ``numberOfCarsPartial`` (integer car counts) exist in the
    XSD but have no engine box — omitted (all ``minOccurs=0``).
  - CSV row symbol for the 24% era: the official CSV-format PDF enumerates only
    up to ``KMD5`` (its newest, "01.2025 and later") and has NO ``transactions24``
    column. Our data is 24%-era (KMD6). We emit the row under the resolved
    version symbol with ``transactions24`` leading, per CSV-format rule 11
    ("the sequence of data elements coincides with the XML scheme"). The literal
    KMD6 CSV symbol/column list is UNVERIFIED against an official sample.
"""
from __future__ import annotations

# --- XML envelope ------------------------------------------------------------
# vatDeclaration has NO namespace (no targetNamespace in the XSD).
KMD_ROOT_ELEMENT = "vatDeclaration"

KMD_EL_TAXPAYER_REGCODE = "taxPayerRegCode"
KMD_EL_SUBMITTER_PERSON_CODE = "submitterPersonCode"
KMD_EL_YEAR = "year"
KMD_EL_MONTH = "month"
KMD_EL_DECLARATION_TYPE = "declarationType"
KMD_EL_VERSION = "version"
KMD_EL_DECLARATION_BODY = "declarationBody"
KMD_EL_SALES_ANNEX = "salesAnnex"
KMD_EL_PURCHASES_ANNEX = "purchasesAnnex"

# declarationBody mandatory boolean flags (order per XSD/example).
KMD_EL_NO_SALES = "noSales"
KMD_EL_NO_PURCHASES = "noPurchases"
KMD_EL_SUM_PER_PARTNER_SALES = "sumPerPartnerSales"
KMD_EL_SUM_PER_PARTNER_PURCHASES = "sumPerPartnerPurchases"

# --- Per-box element mapping -------------------------------------------------
# (engine box_code -> real declarationBody element local-name), in the exact
# KMD6 emission order of the official example. Boxes computed by e-MTA
# (KMD_COMPUTED_BOXES) are absent here. Legacy/count elements with no engine
# box (selfSupply20/selfSupply9/numberOfCars/numberOfCarsPartial) are omitted.
KMD_BODY_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("1", "transactions24"),      # 24% standard (KMD line 1, from 07.2025)
    ("1-2", "transactions22"),    # 22% legacy (KMD line 1²)
    ("1-1", "transactions20"),    # 20% legacy (KMD line 1¹)
    ("2", "transactions9"),       # 9% (line 2)
    ("2-1", "transactions5"),     # 5% (line 2¹)
    ("2-2", "transactions13"),    # 13% (line 2²)
    ("3", "transactionsZeroVat"),  # 0% total (line 3)
    ("3.1", "euSupplyInclGoodsAndServicesZeroVat"),  # line 3.1
    ("3.1.1", "euSupplyGoodsZeroVat"),               # line 3.1.1
    ("3.2", "exportZeroVat"),                        # line 3.2
    ("3.2.1", "salePassengersWithReturnVat"),        # line 3.2.1
    ("5", "inputVatTotal"),        # line 5 (deductible input VAT total)
    ("5.1", "importVat"),          # line 5.1
    ("5.2", "fixedAssetsVat"),     # line 5.2
    ("5.3", "carsVat"),            # line 5.3 (100% business car)
    ("5.4", "carsPartialVat"),     # line 5.4 (partial business car)
    ("6", "euAcquisitionsGoodsAndServicesTotal"),    # line 6
    ("6.1", "euAcquisitionsGoods"),                  # line 6.1
    ("7", "acquisitionOtherGoodsAndServicesTotal"),  # line 7
    ("7.1", "acquisitionImmovablesAndScrapMetalAndGold"),  # line 7.1
    ("8", "supplyExemptFromTax"),        # line 8
    ("9", "supplySpecialArrangements"),  # line 9
    ("10", "adjustmentsPlus"),           # line 10
    ("11", "adjustmentsMinus"),          # line 11
)

# Engine boxes with NO declarationBody element — computed by e-MTA, dropped
# from the submitted file. "4-1" is UNVERIFIED (see module docstring).
KMD_COMPUTED_BOXES: frozenset[str] = frozenset({"4", "4-1", "12", "13"})

# Ordered engine box codes that ARE emitted (single source of truth for both
# XML element order and CSV column order — CSV-format rule 11).
KMD_EMITTED_BOX_ORDER: tuple[str, ...] = tuple(bc for bc, _ in KMD_BODY_ELEMENTS)
KMD_FIELD_NAMES: dict[str, str] = {bc: el for bc, el in KMD_BODY_ELEMENTS}

assert len(KMD_EMITTED_BOX_ORDER) == 24, "24 filable KMD boxes (28 − 4 computed)"

# --- CSV ---------------------------------------------------------------------
# Row-symbol-multiplexed CSV (kmd_description_of_csv_format_2025.pdf): each row
# starts with a symbol (header / KMD5 / A / B). NO column-name header row — the
# symbol identifies the row. UTF-8, ';' separator, no trailing ';', CRLF, '.'
# or ',' decimal (we use '.'). The optional ``header`` row is M2M-only; the
# manual-upload path omits it.
KMD_CSV_ENCODING = "utf-8"
KMD_CSV_DELIMITER = ";"
KMD_CSV_HEADER_SYMBOL = "header"
KMD_CSV_SALES_SYMBOL = "A"       # Part A sales-invoice row
KMD_CSV_PURCHASES_SYMBOL = "B"   # Part B purchase-invoice row
# Body row symbol is the resolved version (KMD4/KMD5/KMD6) — see
# serializer.KmdReportingContext.resolved_version + module docstring UNVERIFIED.

# CSV body is HEADERLESS and POSITIONAL — the row symbol is the only header, so
# every column must sit at its documented slot (CSV-format rule 10: an unfilled
# *middle* element still emits its ';'). The documented KMD5 body row carries
# the two integer car-count columns ``numberOfCars`` (after ``carsVat``, box 5.3)
# and ``numberOfCarsPartial`` (after ``carsPartialVat``, box 5.4). Our engine has
# no value for either, so they are emitted as EMPTY fields at their exact
# positions — dropping them would shift every later column left. Each entry is
# ``("box", box_code)`` (emit the money value) or ``("empty", name)`` (emit "").
def _csv_body_columns() -> tuple[tuple[str, str], ...]:
    cols: list[tuple[str, str]] = []
    for box_code, _element in KMD_BODY_ELEMENTS:
        cols.append(("box", box_code))
        if box_code == "5.3":
            cols.append(("empty", "numberOfCars"))
        elif box_code == "5.4":
            cols.append(("empty", "numberOfCarsPartial"))
    return tuple(cols)


KMD_CSV_BODY_COLUMNS: tuple[tuple[str, str], ...] = _csv_body_columns()

# --- Version resolution (KMD4/KMD5/KMD6 by taxable period) --------------------
# Per the XSD ``version`` annotation + example: KMD4 = 01.2024..12.2024,
# KMD5 = 01.2025..06.2025, KMD6 = 07.2025 onward.
def resolve_kmd_version(year: int, month: int) -> str:
    if (year, month) >= (2025, 7):
        return "KMD6"
    if (year, month) >= (2025, 1):
        return "KMD5"
    if (year, month) >= (2024, 1):
        return "KMD4"
    return "KMD4"  # pre-2024 out of scope; KMD4 is the oldest we emit
