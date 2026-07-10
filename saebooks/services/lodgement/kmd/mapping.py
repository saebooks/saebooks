"""EE KMD (VAT return) box-code -> e-MTA manual-upload field-name mapping.

⚠ CONFORMANCE STATUS — read before trusting the output
------------------------------------------------------
Every name below — the XML root element, its namespace/prefix/schemaRef,
each per-box element local-name, and the CSV column headers/delimiter — is a
**PLACEHOLDER**. Per the scope
(``~/.claude/plans/kmd-formula-support-scope.md`` §5, Packet 4): "the
'Deklaratsiooni ja aruannete esitamise tehniline info' page is JS-rendered
and did not yield the XSD via curl; the schema must be fetched from e-MTA's
technical-info / X-tee catalogue before coding." This module follows the
same mitigation ``services/lodgement/sbr/bas.py`` used for the ATO AS
taxonomy: ship structurally-correct PLACEHOLDER names, centralised in this
ONE file, pinned by a golden-file test
(``tests/services/lodgement/test_kmd_serializer.py``), so dropping the real
e-MTA element/column names in later is a mechanical, reviewable diff — no
logic in ``serializer.py`` changes.

Box codes and their (Estonian) meanings are NOT placeholder — they are
sourced from the scope's §2 box-by-box disposition table, itself citing the
[FORM] reverse-side instructions and [EMTA-FILL]. ``KMD_BOX_ORDER`` is the
form's own 28-box display order (Lahter 1 .. Lahter 13), matching the EE
seed's ``display_order`` 1..28 in
``saebooks/seeds/jurisdictions/EE/tax_return_box_definitions.yaml`` — the
seed's internal-only helper boxes (``1_DOMESTIC``/``1_RC``/``5_DOMESTIC``/
``5_RC``, ``display_order`` >= 100, feeding the box-1/box-5 BOX-FORMULA
introduced in Packet 3) are deliberately NOT in this mapping: they are
engine-internal aggregation legs, not fields on the filable form.
"""
from __future__ import annotations

# --- XML PLACEHOLDERS -------------------------------------------------------
# ⚠ TODO(e-MTA XSD): replace with the real namespace/schemaRef once fetched
# from e-MTA's technical-info page / X-tee catalogue (scope §5 UNVERIFIED item).
KMD_TAXONOMY_NS = "urn:emta:PLACEHOLDER:kmd"
KMD_TAXONOMY_PREFIX = "kmd"
KMD_SCHEMA_REF = "urn:emta:PLACEHOLDER:kmd.xsd"
KMD_ROOT_ELEMENT = "KmdDeklaratsioon"

# Root-level attribute names (also PLACEHOLDER — real e-MTA XSD may use
# elements instead of attributes for these; structure-correct, not verified).
KMD_ATTR_REGCODE = "regkood"
KMD_ATTR_PERIOD_START = "perioodAlgus"
KMD_ATTR_PERIOD_END = "perioodLopp"

# --- CSV PLACEHOLDERS --------------------------------------------------------
# ⚠ TODO(e-MTA CSV spec): delimiter/encoding/column-order are PLACEHOLDER —
# semicolon chosen only because it is the common Estonian/EU CSV convention
# (avoids collision with comma-decimal locales); NOT verified against any
# e-MTA sample file.
KMD_CSV_DELIMITER = ";"
KMD_CSV_ENCODING = "utf-8"
KMD_CSV_HEADER_REGCODE = "regkood"
KMD_CSV_HEADER_PERIOD_START = "periood_algus"
KMD_CSV_HEADER_PERIOD_END = "periood_lopp"

# --- Per-box field names (shared by XML element local-names and CSV column
# headers, so there is exactly one name-per-box to correct later) -----------
# ⚠ PLACEHOLDER local-names — modelled on the box codes / [FORM] Estonian
# labels, NOT sourced from the real XSD. Ordering below IS load-bearing
# (KMD_BOX_ORDER, derived from this dict's insertion order) — it mirrors the
# form's own Lahter 1..13 sequence, the EE seed's display_order 1..28.
KMD_FIELD_NAMES: dict[str, str] = {
    "1": "Lahter1_Maksustatav24",
    "1-1": "Lahter1a_Maksustatav20Legacy",
    "1-2": "Lahter1b_Maksustatav22Legacy",
    "2": "Lahter2_Maksustatav9",
    "2-1": "Lahter2a_Maksustatav5Legacy",
    "2-2": "Lahter2b_Maksustatav13",
    "3": "Lahter3_Maksustatav0",
    "3.1": "Lahter3_1_UhendusesiseneKaibeKokku",
    "3.1.1": "Lahter3_1_1_KaubaUhendusesineKaive",
    "3.2": "Lahter3_2_KaubaEksport",
    "3.2.1": "Lahter3_2_1_TaxFreeReisijaMuuk",
    "4": "Lahter4_KaibemaksKokku",
    "4-1": "Lahter4a_ImpordiKaibemaks",
    "5": "Lahter5_SisendkaibemaksKokku",
    "5.1": "Lahter5_1_ImpordiSisendkaibemaks",
    "5.2": "Lahter5_2_PohivaraSisendkaibemaks",
    "5.3": "Lahter5_3_SoiduautoSisendkaibemaks100",
    "5.4": "Lahter5_4_SoiduautoSisendkaibemaks50",
    "6": "Lahter6_UhendusesineSoetamineKokku",
    "6.1": "Lahter6_1_KaubaUhendusesineSoetamine",
    "7": "Lahter7_MuuSoetamine",
    "7.1": "Lahter7_1_ErikordSoetamine",
    "8": "Lahter8_MaksuvabaKaive",
    "9": "Lahter9_ErikordKaive",
    "10": "Lahter10_TapsustusedPlus",
    "11": "Lahter11_TapsustusedMiinus",
    "12": "Lahter12_TasumiseleKuuluv",
    "13": "Lahter13_Enammakstud",
}

# The form's own box order — the single source of truth for both the XML
# element order and the CSV column order.
KMD_BOX_ORDER: tuple[str, ...] = tuple(KMD_FIELD_NAMES.keys())

assert len(KMD_BOX_ORDER) == 28, "KMD main form has exactly 28 boxes (scope §2)"
