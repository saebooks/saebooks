"""EE cashbook profile — the Estonian bolt-on for the cashbook edition.

Estonian micro-business (FIE / väike-OÜ) cashbook: EUR, käibemaks-aware.
Mirrors the AU Gate-1 taxonomy's SHAPE (same dataclass, same resolver
mechanics) with Estonian categories wired to the EE standard chart
(``seeds/jurisdictions/EE/chart_template.yaml``) and the EE tax-code series
(``STD`` standard-rate käibemaks, ``INPUT_STD`` deductible input VAT,
``INPUT_EXEMPT`` financial services, ``NTR`` not reportable).

Rate note: ``gst_default`` snapshots the CURRENT standard käibemaks rate
(24% since 2025-07-01) exactly as the AU table snapshots 10% GST — the
authoritative dated series lives in the reference data; the cashbook uses
the snapshot for gross→net splits on inclusive amounts and the per-company
TaxCode row (resolved by code) carries KMD reporting. Registered from
``jurisdictions/ee/__init__.py`` via
``services.cashbook_categories.register_cashbook_profile`` (Job C shape).

Labels are Estonian-first — this is the Estonian service's picker; the web
layer does not gettext-translate engine data.
"""
from __future__ import annotations

from decimal import Decimal

from saebooks.services.cashbook_categories import (
    CashbookCategory,
    CashbookJurisdictionProfile,
)

_KM = Decimal("0.24")  # standard käibemaks 24% (since 2025-07-01)
_ZERO = Decimal("0")

EE_CATEGORIES: tuple[CashbookCategory, ...] = (
    # ---------- Tulud (income) ----------
    CashbookCategory(
        code="INC_SALES",
        label="Müügitulu — kaubad",
        group="income",
        direction="income",
        default_account_code="4200",
        gst_default=_KM,
        tax_code="STD",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="INC_SERVICES",
        label="Müügitulu — teenused",
        group="income",
        direction="income",
        default_account_code="4100",
        gst_default=_KM,
        tax_code="STD",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="INC_EXPORT",
        label="Eksport ja ühendusesisene käive",
        group="income",
        direction="income",
        default_account_code="4400",
        gst_default=_ZERO,
        tax_code="ZERO_EXPORT",
        reporting_type="export",
        hint_text="0% käibemaks — eksport või ühendusesisene käive (KMD lahter 3).",
    ),
    CashbookCategory(
        code="INC_INTEREST",
        label="Intressitulu",
        group="income",
        direction="income",
        default_account_code="4900",
        gst_default=_ZERO,
        tax_code=None,
        reporting_type="out_of_scope",
        hint_text="Käibemaksuvaba. Pangaintress kontole.",
    ),
    CashbookCategory(
        code="INC_OTHER",
        label="Muud äritulud",
        group="income",
        direction="income",
        default_account_code="4300",
        gst_default=_KM,
        tax_code="STD",
        reporting_type="taxable",
    ),
    # ---------- Kulud (expenses) ----------
    CashbookCategory(
        code="EXP_MATERIALS",
        label="Kaubad ja materjalid",
        group="materials",
        direction="expense",
        default_account_code="5100",
        gst_default=_KM,
        tax_code="INPUT_STD",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_RENT",
        label="Üür",
        group="home_office",
        direction="expense",
        default_account_code="6200",
        gst_default=_KM,
        tax_code="INPUT_STD",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_UTILITIES",
        label="Kommunaalkulud",
        group="home_office",
        direction="expense",
        default_account_code="6300",
        gst_default=_KM,
        tax_code="INPUT_STD",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_TELCO",
        label="Side ja internet",
        group="telco",
        direction="expense",
        default_account_code="6400",
        gst_default=_KM,
        tax_code="INPUT_STD",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_VEHICLE",
        label="Sõidukikulud",
        group="vehicle",
        direction="expense",
        default_account_code="6500",
        gst_default=_KM,
        tax_code="INPUT_STD",
        reporting_type="taxable",
        hint_text="Sõiduauto kuludelt on sisendkäibemaks üldjuhul 50% "
        "mahaarvatav — kontrolli oma raamatupidajaga.",
    ),
    CashbookCategory(
        code="EXP_BANK_FEES",
        label="Pangakulud",
        group="bank",
        direction="expense",
        default_account_code="6600",
        gst_default=_ZERO,
        tax_code="INPUT_EXEMPT",
        reporting_type="input_taxed",
        hint_text="Finantsteenused on käibemaksuvabad.",
    ),
    CashbookCategory(
        code="EXP_WAGES",
        label="Palgakulu",
        group="other_expense",
        direction="expense",
        default_account_code="6100",
        gst_default=_ZERO,
        tax_code=None,
        reporting_type="out_of_scope",
        hint_text="Palgad käibemaksuga ei maksustata. Tööjõumaksud (TSD) "
        "deklareeritakse eraldi.",
    ),
    CashbookCategory(
        code="EXP_OTHER",
        label="Muud tegevuskulud",
        group="other_expense",
        direction="expense",
        default_account_code="6300",
        gst_default=_KM,
        tax_code="INPUT_STD",
        reporting_type="taxable",
    ),
    # ---------- Ülekanded (special) ----------
    CashbookCategory(
        code="TX_TRANSFER",
        label="Ülekanne kontode vahel",
        group="transfer",
        direction="transfer",
        default_account_code=None,
        gst_default=_ZERO,
        tax_code=None,
        reporting_type="out_of_scope",
        hint_text="Kasumiaruannet ei mõjuta. Liigutab raha kahe oma "
        "pangakonto vahel; ei ole tulu ega kulu.",
    ),
)

EE_CASHBOOK_PROFILE = CashbookJurisdictionProfile(
    jurisdiction="EE",
    currency="EUR",
    categories=EE_CATEGORIES,
)
