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

# =============================================================================
# Module 1 (ee-frontier-build-plan.md §"MODULE 1") — Lisa 2-7 element names.
#
# CSV FINDING (worth flagging prominently — corrects the build-plan's own
# "add per-annex CSV column tables" instruction at 1.1): read
# ``csv_tsd_failiformaadid_01.01.2025_eng.pdf`` in full. It pins CSV column
# tables for exactly FOUR subforms: "Annex1 subform 1a" (Lisa 1 A, already
# shipped), "Annex1 subform 2" (Lisa 1 ship-crew, not shipped),
# "Annex2 subform 1a" (Lisa 2 A — the ONLY Lisa 2-7 subform with a real,
# pinned CSV spec), and "Annex2 subform 3" (Lisa 2 non-resident ship crew /
# ``tsd_L2_3A``, not modelled here — see generator.py). There is NO official
# CSV column table anywhere in the package for Lisa 2 B, Lisa 2 investment
# funds, or ANY of Lisa 3/4/5/6/7. Rather than invent one (the exact
# PLACEHOLDER anti-pattern this codebase's own conformance-status docstrings
# elsewhere warn against), this module emits CSV ONLY for Lisa 2 A
# (``TSD_LISA2_A_CSV_COLUMNS`` below, pinned to the real spec) and leaves
# every other Lisa 2-7 subform XML-ONLY — the XSD-anchored XML is the
# authoritative wire format regardless; CSV is a secondary convenience the
# e-MTA package itself does not offer for these annexes.
# =============================================================================

# --- Lisa 2 (non-resident payments/withholding) ------------------------------
TSD_EL_LISA2 = "tsd_L2_0"
TSD_EL_A2_ISIK_LIST = "aIsikList"
TSD_EL_A2_ISIK = "tsd_L2_A_Isik"
TSD_EL_B2_ISIK_LIST = "bIsikList"
TSD_EL_B2_ISIK = "tsd_L2_B_Isik"
# NOTE: "vmList" is reused verbatim under A-Isik / B-Isik / Inv_Fond, same
# literal value as the Lisa-1 ``TSD_EL_VM_LIST`` already imported above —
# no new constant here, reuse that one (both are literally "vmList" in the
# XSD; different parent elements, same child element name).
TSD_EL_A2_VM = "tsd_L2_A_Vm"
TSD_EL_B2_VM = "tsd_L2_B_Vm"
TSD_EL_MVT_LIST = "mvtList"        # reused under A-Vm / B-Vm
TSD_EL_A2_MVT = "tsd_L2_A_Mvt"
TSD_EL_B2_MVT = "tsd_L2_B_Mvt"
TSD_EL_INVFOND_LIST = "invFondList"
TSD_EL_INVFOND = "tsd_L2_2_Inv_Fond"
TSD_EL_INVFOND_VM = "tsd_L2_2_Vm"
TSD_EL_ISIK_KOOD_2A = "c2000_Kood"
TSD_EL_ISIK_NIMI_2A = "c2010_Nimi"
TSD_EL_ISIK_KOOD_2B = "c2300_Kood"
TSD_EL_ISIK_NIMI_2B = "c2310_Nimi"

# (row attr, element) — Lisa 2 A payment (tsd_L2_A_Vm), XSD order.
TSD_LISA2_A_VM_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("country_code", "c2020_RiikKood"),
    ("payment_type_code", "c2030_ValiKood"),
    ("gross", "c2040_Summa"),
    ("a1_certificate_country_code", "c2060_RiikKood"),
    ("social_tax_base", "c2070_Smvm"),
    ("incapacity_pension_deducted", "c2080_TvpVah"),
    ("prior_month_rate_deducted", "c2090_KuumVah"),
    ("minimum_social_tax_increase", "c2100_KuumSuur"),
    ("social_tax", "c2110_Sm"),
    ("unemployment_base", "c2120_Tkvm"),
    ("unemployment_employee", "c2130_Tk"),
    ("unemployment_employer", "c2140_Ttk"),
    ("income_tax_base", "c2150_Tmvm"),
    ("income_tax_rate", "c2160_TmMaar"),
    ("income_tax", "c2170_Tm"),
)
TSD_LISA2_A_MVT_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("source_code", "c2154_TuliKood"),
    ("amount", "c2155_Summa"),
)
# (row attr, element) — Lisa 2 B payment (tsd_L2_B_Vm), XSD order.
TSD_LISA2_B_VM_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("payment_type_code", "c2320_ValiKood"),
    ("gross", "c2330_Summa"),
    ("year", "c2340_Aasta"),
    ("month", "c2350_Kuu"),
    ("reason_code", "c2360_Pohjus"),
    ("social_tax_base", "c2370_Smvm"),
    ("social_tax_base_deducted", "c2380_SmvmVah"),
    ("social_tax_base_increase", "c2390_SmvmSuur"),
    ("social_tax_base_adjustment", "c2400_SmvmSk"),
    ("social_tax", "c2410_Sm"),
    ("unemployment_base", "c2420_Tkvm"),
    ("unemployment_employee", "c2430_Tk"),
    ("unemployment_employer", "c2440_Ttk"),
    ("income_tax_base", "c2450_Tmvm"),
    ("income_tax_rate", "c2460_TmMaar"),
    ("income_tax", "c2470_Tm"),
)
TSD_LISA2_B_MVT_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("source_code", "c2454_TuliKood"),
    ("amount", "c2455_Summa"),
)
TSD_EL_B2_REASON_EXPLANATION = "pohjusSelgitus"   # trailing element, after mvtList

# (row attr, element) — Lisa 2 investment-fund header (tsd_L2_2_Inv_Fond).
TSD_LISA2_INVFOND_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("fund_code", "c2700_Kood"),
    ("fund_name", "c2710_Nimi"),
    ("fund_country_code", "c2720_RiikKood"),
    ("manager_code", "c2730_FvKood"),
    ("manager_name", "c2740_FvNimi"),
    ("manager_country_code", "c2750_FvRiikKood"),
    ("participation_percent", "c2780_Osalus"),
)
TSD_LISA2_INVFOND_VM_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("payment_type_code", "c2760_ValiKood"),
    ("amount", "c2770_Summa"),
    ("income_tax", "c2790_Tm"),
)
# (totals attr, element) — Lisa 2 annex-level roll-up, XSD order.
TSD_LISA2_TOTALS_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("social_tax_base_a", "c2200_Smvm"),
    ("social_tax_a", "c2210_Sm"),
    ("unemployment_employee_a", "c2220_Tk"),
    ("unemployment_employer_a", "c2230_Ttk"),
    ("income_tax_base_a", "c2240_Tmvm"),
    ("income_tax_a", "c2250_Tm"),
    ("social_tax_base_b", "c2500_Smvm"),
    ("social_tax_b", "c2510_Sm"),
    ("unemployment_employee_b", "c2520_Tk"),
    ("unemployment_employer_b", "c2530_Ttk"),
    ("income_tax_base_b", "c2540_Tmvm"),
    ("income_tax_b", "c2550_Tm"),
    ("inv_fond_income_tax", "c2800_InvTm"),
)
# xs:long (integer, no decimal formatting) fields across Lisa 2 — used by the
# serializer to route the right formatter (see serializer.py's ⚠ int-vs-
# decimal note). Lisa 2 has none directly (year/month live on the B row, see
# TSD_LISA2_INT_FIELDS below) — kept for symmetry with L6/L7's own sets.
TSD_LISA2_INT_FIELDS: frozenset[str] = frozenset({"year", "month"})

# CSV (the ONLY Lisa 2-7 subform with a real pinned column table — "Annex2
# subform 1a" in csv_tsd_failiformaadid_01.01.2025_eng.pdf). Deliberately
# excludes the deviation columns 2050 (part-time rate) and 2154_610/2154_650
# (income-exemption breakdowns) — not modelled as row fields (no source in
# TsdLisa2ARow); the XML export carries the full column set, CSV is the
# convenience path and is allowed to be a subset per the spec's own
# "columns present in export in addition to import" framing.
TSD_LISA2_A_CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("isikukood", "2000"),
    ("name", "2010"),
    ("country_code", "2020"),
    ("payment_type_code", "2030"),
    ("gross", "2040"),
    ("a1_certificate_country_code", "2060"),
    ("social_tax_base", "2070"),
    ("incapacity_pension_deducted", "2080"),
    ("prior_month_rate_deducted", "2090"),
    ("minimum_social_tax_increase", "2100"),
    ("social_tax", "2110"),
    ("unemployment_base", "2120"),
    ("unemployment_employee", "2130"),
    ("unemployment_employer", "2140"),
    ("income_tax_base", "2150"),
    ("income_tax_rate", "2160"),
    ("income_tax", "2170"),
)
TSD_LISA2_A_CSV_TEXT_CODES = frozenset({"2000", "2010", "2020", "2030", "2060"})

# --- Lisa 3 (special: PE / CFC / disguised distribution) --------------------
# ABSENT from the official example (generator.py's module section docstring)
# — header scalars only, XSD order.
TSD_EL_LISA3 = "tsd_L3_0"
TSD_LISA3_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("profit_removed_from_pe", "c3000_VKasum"),
    ("profit_treaty_exempt", "c3010_Mv"),
    ("treaty_country_code", "c3020_RiikKood"),
    ("assets_imported", "c3333_ToodudVara"),
    ("exempt_income_total", "c3200_VKokku"),
    ("deductible_tax_total", "c3210_MKokku"),
    ("exempt_income_opening", "c3220_VAlgjaak"),
    ("exempt_income_available", "c3230_VabaV"),
    ("deductible_tax_opening", "c3240_MAlgjaak"),
    ("deductible_tax_available", "c3250_VabaM"),
    ("pe_profit_exempt", "c3260_MvKasumV"),
    ("pe_taxable_profit", "c3270_MKasum"),
    ("pe_income_tax", "c3280_Tm"),
    ("income_tax_reducing_liability", "c3290_MaTmM"),
    ("special_income_tax_payable", "c3300_TmEj"),
    ("exempt_income_closing", "c3310_VJaak"),
    ("deductible_tax_closing", "c3320_MJaak"),
    ("annex3_income_tax", "c3350_Tm"),
    ("loan_disguised_as_distribution", "c3810_AntudLaen"),
    ("cfc_profit", "c3815_AyTulu"),
    ("cfc_income_tax", "c3820_TmKe"),
)

# --- Lisa 4 (fringe benefits / erisoodustused) -------------------------------
TSD_EL_LISA4 = "tsd_L4_0"
TSD_LISA4_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("electricity_expense", "c4000_ElKulu"),
    ("fuel_expense", "c4010_KiKulu"),
    ("housing_benefit", "c4030_Is"),
    ("transport_benefit", "c4040_Ts"),
    ("other_benefit", "c4050_Mv"),
    ("below_market_loan", "c4060_SoLaen"),
    ("market_interest_rate", "c4061_TuruIntr"),
    ("loan_interest_rate", "c4062_LaenIntr"),
    ("below_cost_transfer", "c4070_AllaTh"),
    ("market_value", "c4071_Th"),
    ("sale_price", "c4072_Rh"),
    ("above_cost_acquisition", "c4080_OoTulu"),
    ("acquisition_market_value", "c4081_OoTh"),
    ("acquisition_price", "c4082_ORh"),
    ("acquisition_percent", "c4083_Op"),
    ("above_market_sale", "c4090_YleTh"),
    ("sale_market_value", "c4091_Rh"),
    ("sale_actual_price", "c4092_Th"),
    ("waived_claim", "c4100_LoobuRn"),
    ("business_entertainment_expense", "c4110_KoKulu"),
    ("other_fringe_expense", "c4120_TeKulu"),
    ("special_benefit_expenses", "c4130_MEs"),
    ("total_expenses_incl_vat", "c4140_EsSumma"),
    ("prior_period_income_tax", "c4150_EiTm"),
    ("prior_period_social_tax", "c4160_EiSm"),
    ("special_income_tax", "c4170_TmEj"),
    ("social_tax", "c4180_Sm"),
    ("social_tax_on_expenses", "c4181_SmEs"),
)

# --- Lisa 5 (gifts / donations / entertainment) ------------------------------
TSD_EL_LISA5 = "tsd_L5_0"
TSD_LISA5_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("gifts_total", "c5000_Ki"),
    ("prior_gift_month", "c5010_IKiKuu"),
    ("prior_gift_year", "c5020_IKiAasta"),
    ("deductible_amount", "c5040_IKasSumma"),
    ("ten_percent_cap", "c5050_10Prots"),
    ("taxable_gifts", "c5060_IMs"),
    ("gift_income_tax", "c5070_ITm"),
    ("gift_income_tax_paid", "c5080_ITasTm"),
    ("gift_income_tax_refunded", "c5090_ITagTm"),
    ("prior_business_gift_month", "c5100_KyKuluKuu"),
    ("prior_business_gift_year", "c5110_KyKuluAasta"),
    ("business_gift_base", "c5120_KyIsmv"),
    ("business_gift_income_tax", "c5130_KyTm"),
    ("business_gift_income_tax_paid", "c5140_KyTasTm"),
    ("business_gift_income_tax_refunded", "c5150_KyTagTm"),
    ("special_income_tax_payable", "c5160_TasTmEj"),
    ("tonnage_gifts_total", "c5220_TonnKiKokku"),
)
# ⚠ c5010_IKiKuu / c5020_IKiAasta are xs:decimal in the XSD (NOT xs:long,
# unlike every other month/year pair in this schema — verified by direct
# lookup, not assumed) — kept as Decimal fields in TsdLisa5Header
# accordingly; no int-routing entry needed for Lisa 5.

# --- Lisa 6 (non-business expenses) ------------------------------------------
TSD_EL_LISA6 = "tsd_L6_0"
TSD_EL_L6_1_LIST = "tsd_L6_1List"
TSD_EL_L6_1 = "tsd_L6_1"
TSD_EL_L6_2_LIST = "tsd_L6_2List"
TSD_EL_L6_2 = "tsd_L6_2"
TSD_EL_L6_3_LIST = "tsd_L6_3List"
TSD_EL_L6_3 = "tsd_L6_3"
TSD_LISA6_HEADER_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("related_party_value_diff", "c6000_TVahe"),
    ("fines_penalties", "c6010_Trsr"),
    ("fines_penalties_to_emta", "c6011_ETrsr"),
    ("interest_paid", "c6020_Intr"),
    ("interest_paid_to_emta", "c6021_EIntr"),
    ("seized_assets_value", "c6030_Kvara"),
    ("environmental_charges", "c6040_Kkt"),
    ("environmental_charges_to_emta", "c6041_EKkt"),
    ("bribes_kickbacks", "c6050_Pistis"),
    ("non_business_membership_fees", "c6060_Lm"),
    ("distributions_missing_source_doc", "c6070_PdokVm"),
    ("non_business_expenses_other", "c6080_KvmMuu"),
    ("low_tax_territory_securities_expense", "c6090_Vpk"),
    ("low_tax_territory_ownership_expense", "c6100_Osk"),
    ("low_tax_territory_penalty_damages", "c6110_Kahj"),
    ("low_tax_territory_loan", "c6120_Laen"),
    ("low_tax_territory_credit_loss", "c6130_KrKah"),
    ("tax_base_reduction", "c6140_MsVhnd"),
    ("total_taxable_amount", "c6150_SumKokku"),
    ("income_tax_payable", "c6160_Tasutav"),
    ("tonnage_non_business_total", "c6320_TonnKvmKokku"),
)
TSD_LISA6_ROW1_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("month", "c6141_Kuu"),
    ("year", "c6142_Aasta"),
    ("amount", "c6143_Summa"),
)
TSD_LISA6_ROW2_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("related_party_code", "c6200_Kood"),
    ("related_party_name", "c6210_Nimi"),
    ("country_code", "c6220_RiikKood"),
    ("taxable_amount", "c6230_MSumma"),
    ("payment_type_code", "c6240_ValiKood"),
)
TSD_LISA6_ROW3_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("year", "c6300_Aasta"),
    ("amount", "c6310_Summa"),
)
# xs:long (integer) fields — must NOT go through the 2dp money formatter.
TSD_LISA6_INT_FIELDS: frozenset[str] = frozenset({"month", "year"})

# --- Lisa 7 (dividends / equity payments) ------------------------------------
TSD_EL_LISA7 = "tsd_L7_0"
TSD_EL_L7_1B_LIST = "tsd_L7_1bList"
TSD_EL_L7_1B = "tsd_L7_1b"
TSD_EL_L7_1C_LIST = "tsd_L7_1CList"
TSD_EL_L7_1C = "tsd_L7_1C"
TSD_EL_L7_2_LIST = "tsd_L7_2List"
TSD_EL_L7_2 = "tsd_L7_2"
TSD_EL_L7_2B_LIST = "tsd_L7_2BList"
TSD_EL_L7_2B = "tsd_L7_2B"
TSD_EL_L7_4_LIST = "tsd_L7_4List"
TSD_EL_L7_4 = "tsd_L7_4"
TSD_LISA7_HEADER_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("dividends_total", "c7008_DivKokku"),
    ("hidden_distributions", "c7012_VmKeSum"),
    ("assets_taken_out", "c7014_Lahkumismaks"),
    ("cfc_profit", "c7016_Cfc"),
    ("tonnage_dividends_total", "c7022_TonnDivKokku"),
    ("equity_contributions", "c7030_OmakapSm"),
    ("equity_contributions_total", "c7040_OmakapSmKokku"),
    ("equity_contributions_adjusted", "c7050_OmakapSmKorrig"),
    ("equity_distributions_total", "c7060_OmakapVm"),
    ("equity_undistributed_closing", "c7070_OmakapValjamaksmata"),
    ("taxable_excess_over_equity", "c7080_VmYleSmMaksust"),
    ("foreign_tax_withheld_total", "c7160_VrTasutudTm"),
    ("foreign_tax_withheld_adjusted", "c7170_VrTasutudTmKorrig"),
    ("foreign_tax_used", "c7180_VrVahendus"),
    ("foreign_tax_unused_closing", "c7190_VrVmTmKasutamata"),
    ("income_tax_payable", "c7200_TasutavTm"),
    ("dividend_equity_income_tax", "c7217_DivOmakapTm"),
    ("income_tax_after_foreign_credit", "c7218_TmVrVahendus"),
    ("income_tax_after_credit_institution", "c7219_TmKredasVahendus"),
    ("exempt_income", "c7290_MvVm"),
    ("exempt_income_adjusted", "c7300_MvVmKorrig"),
    ("reduced_rate_dividends_granted_opening", "c7301_MvMmDivAlgus"),
    ("reduced_rate_dividends_received", "c7302_MvMmDivYa"),
    ("tonnage_dividends_received_opening", "c7303_MvTonnDivAlgus"),
    ("tonnage_dividends_received", "c7304_MvTonnDivYa"),
    ("exempt_dividends_paid_total", "c7310_MvVmDiv"),
    ("reduced_rate_dividends_paid", "c7311_MvVmMmDiv"),
    ("exempt_equity_payments", "c7320_MvVmOmakap"),
    ("exempt_income_unused_closing", "c7330_MvVmKasutamata"),
    ("reduced_rate_dividends_unused_closing", "c7331_MvMmDivKasutamata"),
    ("tonnage_dividends_unused_closing", "c7332_MvTonnDivKasutamata"),
)
TSD_LISA7_ROW1B_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("payer_regcode", "c7101_Regkood"),
    ("payer_name", "c7102_Nimi"),
    ("payer_country_code", "c7103_RiikKood"),
    ("income_type_code", "c7110_TuliKood"),
    ("payment_date", "c7120_Kpv"),
    ("foreign_income_amount", "c7130_VrSumma"),
    ("foreign_tax_paid", "c7140_VrTasutudTm"),
    ("liability_reduction_amount", "c7150_KohustVahendSumma"),
)
TSD_LISA7_ROW1C_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("year", "c7020_Aasta"),
    ("amount", "c7021_Summa"),
)
TSD_LISA7_ROW2_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("payer_regcode", "c7201_Regkood"),
    ("payer_name", "c7202_Nimi"),
    ("payer_country_code", "c7203_RiikKood"),
    ("income_type_code", "c7210_TuliKood"),
    ("payment_date", "c7220_Kpv"),
    ("dividend_participation_percent", "c7230_OsalusDiv"),
    ("equity_participation_percent", "c7240_OsalusOmakap"),
    ("amount", "c7250_Summa"),
    ("foreign_tax_paid", "c7260_VrTasutudTm"),
    ("foreign_taxed_profit", "c7270_VrMaksustKasum"),
    ("distributed_amount", "c7280_Mvt"),
)
TSD_LISA7_ROW2B_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("payer_regcode", "c7201_Regkood"),
    ("payer_name", "c7202_Nimi"),
    ("payer_country_code", "c7203_RiikKood"),
    ("income_type_code", "c7210_TuliKood"),
    ("payment_date", "c7220_Kpv"),
    ("disguised_loan_amount", "c7211_TagLaen"),
    ("cfc_funding_amount", "c7215_TagCfc"),
    ("exit_tax_funding_amount", "c7216_TagLahkumismaks"),
    ("amount", "c7212_Summa"),
    ("month", "c7213_Kuu"),
    ("year", "c7214_Aasta"),
    ("distributed_amount", "c7280_Mvt"),
)
TSD_LISA7_ROW4_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("payer_regcode", "c7501_Regkood"),
    ("payer_name", "c7502_Nimi"),
    ("payer_country_code", "c7503_RiikKood"),
    ("cooperative_social_tax", "c7510_AyhSm"),
    ("member_social_tax", "c7520_PtkSm"),
    ("cooperative_foreign_tax_paid", "c7530_AyhVrTasutudTm"),
    ("member_foreign_tax_paid", "c7540_PtkVrTasutudTm"),
    ("cooperative_distributed", "c7550_AyhVmt"),
    ("member_distributed", "c7560_PtkVmt"),
    ("reduced_rate_dividends", "c7580_MmDiv"),
    ("credit_institution_prior_advance", "c7590_KredasEelmAastaAvans"),
    ("tonnage_dividends", "c7581_TonnDiv"),
)
# xs:long (integer) fields across Lisa 7 rows — c7020_Aasta (1C),
# c7213_Kuu/c7214_Aasta (2B). c7120_Kpv/c7220_Kpv are xs:date, handled by
# the serializer's existing ``_xml_text`` date branch, not this set.
TSD_LISA7_INT_FIELDS: frozenset[str] = frozenset({"year", "month"})
