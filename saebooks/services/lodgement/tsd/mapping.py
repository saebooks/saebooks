"""TSD (income + social + withholding tax return) -> e-MTA manual-upload
field-name mapping.

⚠ CONFORMANCE STATUS — read before trusting the output
------------------------------------------------------
Every name below — the XML root element, its namespace/prefix/schemaRef,
the MAIN block + Lisa-1 container/row element names, each per-field
local-name, and the CSV column headers/delimiter — is a **PLACEHOLDER**,
exactly like ``services/lodgement/kmd/mapping.py`` and
``services/lodgement/kmd_inf/mapping.py``'s. Per the scope
(``~/.claude/plans/kmd-inf-tsd-scope.md`` §4/§5): "emta.ee is a
JS-rendered Drupal site ... the machine XML/CSV formats for BOTH annexes
are UNVERIFIED". This module follows the identical mitigation: ship
structurally-correct PLACEHOLDER names, centralised in this ONE file,
pinned by a golden-file test (``tests/services/lodgement/test_tsd_golden.py``),
so dropping the real e-MTA element/column names in later is a mechanical,
reviewable diff — no logic in ``serializer.py`` changes.

**Structural delta from both siblings (scope §4):** KMD is a flat
28-box vector (one row, one element per box). KMD-INF is two
*homogeneous* repeating listings (Part A / Part B, same shape as each
other). TSD is **both at once, in one file** — a small aggregate block
(``MAIN``, one row's worth of totals, shaped like KMD's flat vector) PLUS
a *heterogeneous* repeating listing (``Lisa 1``, one row per payment,
shaped like a KMD-INF part). So this module defines a MAIN field name
set (mirrors ``kmd.mapping.KMD_FIELD_NAMES``) **and** a separate Lisa-1
per-row column name set (mirrors ``kmd_inf.mapping``'s per-part column
sets), plus the Lisa-1 row-container/row element names.

Field *semantics* (which figure goes in which field) are NOT
placeholder — MAIN mirrors ``TsdMainTotals`` (scope §2.2 "MAIN is a
trivial roll-up of Lisa 1"), Lisa 1 mirrors the scope §2.2 Lisa-1
data-contract table + ``TsdLisa1Row`` (generator.py, Packet 4). Only the
on-the-wire *names* are unverified. ``payment_date`` is included as a
Lisa-1 column though not explicitly named in the scope §2.2 table — a
per-row listing needs a per-row date the same way KMD-INF's Part A/B
rows carry ``document_date``; the generator already carries it
(``TsdLisa1Row.payment_date``), so it is exposed here rather than
silently dropped. ``employee_id``/``pay_run_id`` are deliberately
EXCLUDED from the wire columns (internal engine keys, not fields on the
e-MTA form) — they are still captured by the persistence path
(``serializer.persist_tsd_return``), which is not file-shaped.
"""
from __future__ import annotations

# --- XML PLACEHOLDERS -------------------------------------------------------
# ⚠ TODO(e-MTA XSD): replace with the real namespace/schemaRef once fetched
# from e-MTA's technical-info page / X-tee catalogue (scope §5 UNVERIFIED item).
TSD_TAXONOMY_NS = "urn:emta:PLACEHOLDER:tsd"
TSD_TAXONOMY_PREFIX = "tsd"
TSD_SCHEMA_REF = "urn:emta:PLACEHOLDER:tsd.xsd"
TSD_ROOT_ELEMENT = "TsdDeklaratsioon"

# Root-level attribute names (also PLACEHOLDER, mirrors kmd/kmd_inf mapping.py).
TSD_ATTR_REGCODE = "regkood"
TSD_ATTR_PERIOD_START = "perioodAlgus"
TSD_ATTR_PERIOD_END = "perioodLopp"

# MAIN aggregate block + Lisa-1 row-container/row element names (PLACEHOLDER).
TSD_MAIN_ELEMENT = "Pealdeklaratsioon"
TSD_LISA1_CONTAINER_ELEMENT = "Lisa1"
TSD_LISA1_ROW_ELEMENT = "Lisa1Kirje"

# --- CSV PLACEHOLDERS --------------------------------------------------------
# ⚠ TODO(e-MTA CSV spec): delimiter/encoding/column-order are PLACEHOLDER,
# same convention as kmd/kmd_inf mapping.py (semicolon, NOT verified
# against any e-MTA sample file).
TSD_CSV_DELIMITER = ";"
TSD_CSV_ENCODING = "utf-8"
TSD_CSV_HEADER_REGCODE = "regkood"
TSD_CSV_HEADER_PERIOD_START = "periood_algus"
TSD_CSV_HEADER_PERIOD_END = "periood_lopp"

# --- MAIN aggregate field names (shared by XML sub-element local-names and
# the single-row MAIN CSV's column headers) ----------------------------------
# ⚠ PLACEHOLDER local-names. Ordering below IS load-bearing
# (TSD_MAIN_COLUMNS, derived from this dict's insertion order) — mirrors
# ``TsdMainTotals``'s own field order (generator.py).
TSD_MAIN_FIELD_NAMES: dict[str, str] = {
    "employee_count": "TootajateArv",
    "total_gross": "BruttoKokku",
    "total_income_tax": "TulumaksKokku",
    "total_unemployment_employee": "TootuskindlustusTootajaKokku",
    "total_unemployment_employer": "TootuskindlustusTooandjaKokku",
    "total_social_tax": "SotsiaalmaksKokku",
    "total_pillar_ii": "KogumispensionKokku",
}
TSD_MAIN_COLUMNS: tuple[str, ...] = tuple(TSD_MAIN_FIELD_NAMES.keys())

assert len(TSD_MAIN_COLUMNS) == 7, "TsdMainTotals has exactly 7 aggregate fields (generator.py)"

# --- Lisa-1 per-row field names (shared by XML sub-element local-names and
# the multi-row Lisa-1 CSV's column headers) ---------------------------------
# ⚠ PLACEHOLDER local-names — modelled on the scope §2.2 Lisa-1 data-contract
# table / Estonian form vocabulary, NOT sourced from the real XSD. Ordering
# below IS load-bearing (TSD_LISA1_COLUMNS) — mirrors the scope's own table
# order (isikukood, payment-type, gross, exemption, income tax, unemployment
# employee, pillar II, social tax, unemployment employer), then the
# generator's own trailing ``payment_date`` (module docstring above).
TSD_LISA1_FIELD_NAMES: dict[str, str] = {
    "isikukood": "Isikukood",
    "payment_type_code": "ValjamakseLiik",
    "gross": "BruttoSumma",
    "basic_exemption_applied": "MaksuvabaTulu",
    "income_tax": "TulumaksKinnipeetud",
    "unemployment_employee": "TootuskindlustusTootajaOsa",
    "pillar_ii": "Kogumispension",
    "social_tax": "Sotsiaalmaks",
    "unemployment_employer": "TootuskindlustusTooandjaOsa",
    "payment_date": "ValjamakseKuupaev",
}
TSD_LISA1_COLUMNS: tuple[str, ...] = tuple(TSD_LISA1_FIELD_NAMES.keys())

assert len(TSD_LISA1_COLUMNS) == 10, "scope's 9 Lisa-1 fields + payment_date (module docstring)"
