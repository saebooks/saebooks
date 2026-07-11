"""TSD (income + social + withholding tax return) -> official e-MTA
``tsd_vorm`` element mapping.

CONFORMANCE STATUS — PINNED to the real e-MTA format (2026-07)
--------------------------------------------------------------
The former PLACEHOLDER names are replaced with the REAL element names from
``tsd_schema_01.01.2025_eng.xsd`` + the official example
(``tests/fixtures/emta_schemas/tsd_example.xml``). This is the largest reshape
of the three formats — the real TSD is NOT the flat MAIN-block + flat-Lisa1-row
model the placeholder guessed:

* Root is ``tsd_vorm`` in **no namespace**. Mandatory header children (in the
  XSD, which is ``xs:all`` at top level so order is technically free — we follow
  the example order): ``regKood``, ``c108_Aasta`` (year), ``c109_Kuu`` (month),
  ``laadimisViis`` ("L" new / "P" amend), ``vorm`` ("TSD").
* There is NO ``<Pealdeklaratsioon>`` MAIN container. The main-form totals are
  ``cNNN_*`` elements DIRECTLY under ``tsd_vorm``, all ``minOccurs=0`` and all
  "Calculated" (e-MTA derives them; ignored on import). We emit the roll-up we
  have — ``c110_Tm`` (income tax), ``c115_Sm`` (social tax), ``c116_Tk``
  (unemployment premium — **employee + employer merged**, the real field is the
  combined premium), ``c117_Kp`` (funded pension). ``employee_count`` /
  ``total_gross`` have NO main-form code (kept only in persistence).
* Lisa 1 is a TWO-LEVEL nesting: ``tsd_L1_0`` -> ``aIsikList`` ->
  ``tsd_L1_A_Isik`` (one per RESIDENT person: ``c1000_Kood`` isikukood,
  ``c1010_Nimi`` name) -> ``vmList`` -> ``tsd_L1_A_Vm`` (one per payment). The
  generator emits one flat row per person-payment, so the serializer groups by
  isikukood into person -> [payments].

Payment-type reconciliation (tsd_lisa_1_valjamakseliikide_tabel_01.01.2025):
the generator's single ``PAYMENT_TYPE_WAGES`` token maps to official
``c1020_ValiKood`` code **"10"** (employment income / remuneration of a resident
natural person). See ``TSD_PAYMENT_TYPE_MAP``.

⚠ UNVERIFIED / dropped-from-wire (kept explicit):
  - ``basic_exemption_applied`` (maksuvaba tulu): ``tsd_L1_A_Vm`` has NO
    applied-basic-exemption element (it is folded into the calculated
    ``c1170_Tm`` income tax). Dropped from the file; still persisted.
  - ``payment_date``: TSD is a monthly return with no per-payment date field in
    ``tsd_L1_A_Vm``. Dropped from the file; still persisted.
  - ``employee_id`` / ``pay_run_id``: internal engine keys, never on the form.
"""
from __future__ import annotations

# --- tsd_vorm envelope (no namespace) ---------------------------------------
TSD_ROOT_ELEMENT = "tsd_vorm"

TSD_EL_REGKOOD = "regKood"
TSD_EL_YEAR = "c108_Aasta"
TSD_EL_MONTH = "c109_Kuu"
TSD_EL_LOAD_METHOD = "laadimisViis"   # "L" (new) | "P" (amend)
TSD_EL_FORM = "vorm"                  # "TSD"
TSD_LOAD_METHOD_NEW = "L"
TSD_FORM_TSD = "TSD"

# --- MAIN roll-up: (TsdMainTotals attr | synthetic, element) ----------------
# ``_unemployment_total`` is synthesised (employee + employer) in the serializer.
TSD_MAIN_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("total_income_tax", "c110_Tm"),
    ("total_social_tax", "c115_Sm"),
    ("_unemployment_total", "c116_Tk"),
    ("total_pillar_ii", "c117_Kp"),
)

# --- Lisa 1 nesting element names -------------------------------------------
TSD_EL_LISA1 = "tsd_L1_0"
TSD_EL_A_ISIK_LIST = "aIsikList"
TSD_EL_A_ISIK = "tsd_L1_A_Isik"
TSD_EL_ISIK_KOOD = "c1000_Kood"      # isikukood
TSD_EL_ISIK_NIMI = "c1010_Nimi"      # name (optional; omitted — generator has none)
TSD_EL_VM_LIST = "vmList"
TSD_EL_VM = "tsd_L1_A_Vm"

# --- Lisa 1 payment (tsd_L1_A_Vm) columns: (row attr, element), XSD order ----
TSD_VM_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("payment_type_code", "c1020_ValiKood"),   # mapped via TSD_PAYMENT_TYPE_MAP
    ("gross", "c1030_Summa"),
    ("social_tax", "c1100_Sm"),
    ("pillar_ii", "c1110_Kp"),
    ("unemployment_employee", "c1130_Tk"),
    ("unemployment_employer", "c1140_Ttk"),
    ("income_tax", "c1170_Tm"),
)

# generator token -> official c1020_ValiKood classifier code.
# Literal token duplicated from generator.PAYMENT_TYPE_WAGES (mapping stays
# import-light; kept in sync by this comment).
TSD_PAYMENT_TYPE_MAP: dict[str, str] = {
    "PLACEHOLDER_PAYMENT_TYPE_WAGES": "10",  # employment income (resident nat. person)
}


def payment_type_code(token: str) -> str:
    """Map a generator payment-type token to its official ``c1020_ValiKood``.
    Unknown tokens pass through unchanged (surfaces a real code if the generator
    later emits one directly)."""
    return TSD_PAYMENT_TYPE_MAP.get(token, token)


# --- CSV (Annex 1 subform 1a: csv_tsd_failiformaadid_01.01.2025) ------------
# UTF-8 WITH BOM; CRLF; ';' separator; no trailing ';'; text fields quoted;
# decimal separator is a COMMA; header row lists the column CODES.
TSD_CSV_ENCODING = "utf-8"
TSD_CSV_BOM = "﻿"
TSD_CSV_DELIMITER = ";"

# Lisa 1 CSV columns: (row attr | synthetic, column code). Codes per the CSV
# spec's Annex 1 subform 1a table. ``payment_type_code`` mapped; calculated
# columns (1100/1110/1130/1140/1170) included (export-style; ignored on import).
TSD_LISA1_CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("isikukood", "1000"),
    ("payment_type_code", "1020"),
    ("gross", "1030"),
    ("social_tax", "1100"),
    ("pillar_ii", "1110"),
    ("unemployment_employee", "1130"),
    ("unemployment_employer", "1140"),
    ("income_tax", "1170"),
)

# MAIN CSV columns (main-form codes). The official CSV spec is annex-focused;
# the main-form header CSV is emitted in the same code-header style.
TSD_MAIN_CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("_year", "108"),
    ("_month", "109"),
    ("total_income_tax", "110"),
    ("total_social_tax", "115"),
    ("_unemployment_total", "116"),
    ("total_pillar_ii", "117"),
)
