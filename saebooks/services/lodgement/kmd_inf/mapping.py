"""KMD-INF (VAT-return invoice annex) -> e-MTA manual-upload field-name
mapping.

⚠ CONFORMANCE STATUS — read before trusting the output
------------------------------------------------------
Every name below — the XML root element, its namespace/prefix/schemaRef,
the Part A/B container + row element names, each per-column local-name,
and the CSV column headers/delimiter — is a **PLACEHOLDER**, exactly like
``services/lodgement/kmd/mapping.py``'s. Per the scope
(``~/.claude/plans/kmd-inf-tsd-scope.md`` §4/§5): "emta.ee is a
JS-rendered Drupal site ... the machine XML/CSV formats for BOTH annexes
are UNVERIFIED". This module follows the identical mitigation: ship
structurally-correct PLACEHOLDER names, centralised in this ONE file,
pinned by golden-file tests (``tests/services/lodgement/test_kmd_inf_*``),
so dropping the real e-MTA element/column names in later is a mechanical,
reviewable diff — no logic in ``serializer.py`` changes.

**Structural delta from ``kmd/mapping.py`` (scope §4):** KMD is a flat
28-box vector — one element/column per box. KMD-INF is a **repeating-row**
document — a header block (regcode, period) plus a list of Part A rows
and a list of Part B rows, each row a fixed set of columns. So this
module defines, per part, a header-field name set *and* a per-row column
name set, plus the row container/row element names, rather than a single
flat field dict.

Column *semantics* (which figure goes in which column) are NOT
placeholder — they come from the scope's §2.1 Part A/B tables, themselves
citing [SEED-EE]. Only the on-the-wire *names* are unverified.
"""
from __future__ import annotations

# --- XML PLACEHOLDERS -------------------------------------------------------
# ⚠ TODO(e-MTA XSD): replace with the real namespace/schemaRef once fetched
# from e-MTA's technical-info page / X-tee catalogue (scope §5 UNVERIFIED item).
KMD_INF_TAXONOMY_NS = "urn:emta:PLACEHOLDER:kmdinf"
KMD_INF_TAXONOMY_PREFIX = "kmdinf"
KMD_INF_SCHEMA_REF = "urn:emta:PLACEHOLDER:kmdinf.xsd"
KMD_INF_ROOT_ELEMENT = "KmdInfDeklaratsioon"

# Root-level attribute names (also PLACEHOLDER, mirrors kmd/mapping.py).
KMD_INF_ATTR_REGCODE = "regkood"
KMD_INF_ATTR_PERIOD_START = "perioodAlgus"
KMD_INF_ATTR_PERIOD_END = "perioodLopp"

# Part A / Part B row-container + per-row element names (PLACEHOLDER).
KMD_INF_PART_A_CONTAINER_ELEMENT = "OsaA"
KMD_INF_PART_A_ROW_ELEMENT = "OsaAKirje"
KMD_INF_PART_B_CONTAINER_ELEMENT = "OsaB"
KMD_INF_PART_B_ROW_ELEMENT = "OsaBKirje"

# --- CSV PLACEHOLDERS --------------------------------------------------------
# ⚠ TODO(e-MTA CSV spec): delimiter/encoding/column-order are PLACEHOLDER,
# same convention as kmd/mapping.py (semicolon — common EE/EU CSV choice,
# NOT verified against any e-MTA sample file). Every data row repeats the
# header regcode/period as its leading three columns — each row is
# self-describing (a repeating-row bulk-upload file, unlike KMD's single
# summary row) — PLACEHOLDER convention, not sourced from a real sample.
KMD_INF_CSV_DELIMITER = ";"
KMD_INF_CSV_ENCODING = "utf-8"
KMD_INF_CSV_HEADER_REGCODE = "regkood"
KMD_INF_CSV_HEADER_PERIOD_START = "periood_algus"
KMD_INF_CSV_HEADER_PERIOD_END = "periood_lopp"

# --- Part A per-row field names (shared by XML sub-element local-names and
# CSV column headers, so there is exactly one name-per-column to correct
# later) -----------------------------------------------------------------
# ⚠ PLACEHOLDER local-names — modelled on the scope §2.1 Part A column
# table / Estonian form vocabulary, NOT sourced from the real XSD.
# Ordering below IS load-bearing (KMD_INF_PART_A_COLUMNS, derived from this
# dict's insertion order) — mirrors the scope's own column table order.
KMD_INF_PART_A_FIELD_NAMES: dict[str, str] = {
    "row_no": "JrkNr",
    "partner_registration_number": "PartneriKood",
    "partner_name": "PartneriNimi",
    "document_number": "ArveNumber",
    "document_date": "ArveKuupaev",
    "document_total_ex_vat": "ArveSummaKm",
    "taxable_value": "MaksustatavVaartus",
    "rate": "Maksumaar",
    "kmd_box_code": "KmdLahter",
    "erisuse_kood": "ErisuseKood",
    "is_credit_note": "KreeditArve",
}
KMD_INF_PART_A_COLUMNS: tuple[str, ...] = tuple(KMD_INF_PART_A_FIELD_NAMES.keys())

# --- Part B per-row field names --------------------------------------------
KMD_INF_PART_B_FIELD_NAMES: dict[str, str] = {
    "row_no": "JrkNr",
    "partner_registration_number": "PartneriKood",
    "partner_name": "PartneriNimi",
    "document_number": "ArveNumber",
    "document_date": "ArveKuupaev",
    "document_total_incl_vat": "ArveSummaKoosKm",
    "input_vat": "SisendKaibemaks",
    "rate": "Maksumaar",
    "erisuse_kood": "ErisuseKood",
}
KMD_INF_PART_B_COLUMNS: tuple[str, ...] = tuple(KMD_INF_PART_B_FIELD_NAMES.keys())
