"""Default cashbook category taxonomy for sole-trader UX.

The cashbook edition (see ``docs/cashbook-edition-design.md``) hides
the chart of accounts from the user behind a fixed picker of
~20 categories. Each category resolves to one expense or income
account in the company's chart of accounts at runtime via
``default_account_code``, so the same default list works against any
seeded chart.

Per-company overrides live on ``companies.cashbook_categories`` (JSONB)
— the resolver merges defaults with overrides at read time. Adding,
removing or repointing a category for everyone happens here in code,
not in DB rows. Keep this module short.

**Gate 1.** This list is the AU sole-trader tax-correctness contract.
Richard signs it off before any code that uses it is enabled in
production. If the mapping is wrong every customer gets wrong P&L and
broken BAS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

CategoryGroup = Literal[
    "income",
    "vehicle",
    "home_office",
    "insurance",
    "professional",
    "materials",
    "software",
    "telco",
    "super",
    "training",
    "tools",
    "travel",
    "bank",
    "other_expense",
    "capital",
    "personal",
    "transfer",
]


Direction = Literal["income", "expense", "transfer"]


@dataclass(frozen=True)
class CashbookCategory:
    """A single cashbook category — picker entry + tax wiring.

    Attributes
    ----------
    code:
        Stable identifier used in API requests, attachments, and
        per-company override dicts. Uppercase snake-case.
    label:
        Default human-readable label shown in the picker. May be
        overridden per-company.
    group:
        Coarse grouping for picker layout. Pure UI — no business logic
        keys off it.
    direction:
        ``income`` / ``expense`` / ``transfer``. Drives which side of
        the JE the category account lands on.
    default_account_code:
        Lookup key into ``accounts.code``. Resolved per-company at
        runtime. NULL is allowed only for the special TX_TRANSFER
        category (which uses a second bank account, not a P&L row).
    gst_default:
        Default GST rate as a Decimal (``0.10`` = 10%, ``0`` = GST-free
        / not-reportable). Overridable per-entry. The cashbook service
        only generates the DR/CR GST Paid|Collected line when the
        company is GST-registered AND the rate is non-zero.
    hint_text:
        Optional one-liner shown under the picker entry on selection —
        flagging substantiation requirements (logbook, sqm, etc.).
    tax_code:
        The ``tax_codes.code`` string to stamp on the category JE line
        (e.g. ``"GST"``, ``"FRE"``, ``"CAP"``, ``"INP"``). Resolved to a
        per-company ``TaxCode.id`` at JE-build time by
        ``cashbook._resolve_category_tax_code``. ``None`` for categories
        that are not BAS-reportable (drawings, transfers).
    reporting_type:
        Redundant BAS reporting hint (``"taxable"`` / ``"gst_free"`` /
        ``"export"`` / ``"input_taxed"`` / ``"capital"``). Used by the
        resolver fallback when the named ``tax_code`` is absent in a
        tenant that renamed its codes, so the line still lands in the
        right BAS box. The BAS aggregator
        (``services/tax_engine/au.py``) keys every G-label off the
        resolved code's ``reporting_type``.
    """

    code: str
    label: str
    group: CategoryGroup
    direction: Direction
    default_account_code: str | None
    gst_default: Decimal = field(default=Decimal("0.10"))
    hint_text: str | None = None
    tax_code: str | None = None
    reporting_type: str = "taxable"


# Order is the picker order. Income at top, then expenses grouped
# logically, then capital, then drawings, then transfer (special).
DEFAULT_CATEGORIES: tuple[CashbookCategory, ...] = (
    # ---------- Income ----------
    # NOTE: ``default_account_code`` values map to the AU Odoo l10n
    # chart of accounts loaded by ``saebooks.seed.load_au_coa`` — codes
    # are stored hyphenated as ``X-NNNN``. Gaps where the AU CoA has no
    # clean match (Software, Bank fees, Service revenue, Training) are
    # pointed at the closest existing account; Gate 1 captures the
    # decision on whether to extend the seed CoA. Per-company override
    # via ``cashbook_categories.overrides[CODE].account_id`` is the
    # escape hatch in the meantime.
    CashbookCategory(
        code="INC_SALES",
        label="Sales",
        group="income",
        direction="income",
        default_account_code="4-2000",  # Wholesale Sales
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
        hint_text="Goods sold to customers.",
    ),
    CashbookCategory(
        code="INC_SERVICES",
        label="Services",
        group="income",
        direction="income",
        # GAP: no dedicated "Service revenue" line in AU CoA — falls
        # through to wholesale sales until Gate 1 decides to extend.
        default_account_code="4-2000",
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
        hint_text="Labour or services billed to customers.",
    ),
    CashbookCategory(
        code="INC_INTEREST",
        label="Interest received",
        group="income",
        direction="income",
        default_account_code="8-1000",  # Interest Income
        gst_default=Decimal("0"),
        tax_code="INP",
        reporting_type="input_taxed",
        hint_text="GST-free. Bank interest credited to your account.",
    ),
    CashbookCategory(
        code="INC_OTHER",
        label="Other income",
        group="income",
        direction="income",
        default_account_code="4-6000",  # Miscellaneous Income
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
        hint_text="Anything that doesn't fit Sales or Services.",
    ),
    # ---------- Expenses ----------
    CashbookCategory(
        code="EXP_VEHICLE",
        label="Vehicle & fuel",
        group="vehicle",
        direction="expense",
        default_account_code="6-1200",  # Car & Truck Expenses
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
        hint_text="Logbook or cents-per-km method — keep records.",
    ),
    CashbookCategory(
        code="EXP_HOME_OFFICE",
        label="Home office",
        group="home_office",
        direction="expense",
        default_account_code="6-2120",  # Other Business Property
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
        hint_text="Floor-area % or fixed-rate method — keep diary.",
    ),
    CashbookCategory(
        code="EXP_INSURANCE",
        label="Insurance",
        group="insurance",
        direction="expense",
        default_account_code="6-1800",  # Insurance
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_PROFESSIONAL",
        label="Accounting & legal",
        group="professional",
        direction="expense",
        default_account_code="6-2200",  # Legal & Professional Services
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_MATERIALS",
        label="Materials & supplies",
        group="materials",
        direction="expense",
        default_account_code="5-5000",  # Materials & Supplies
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_SOFTWARE",
        label="Software & subscriptions",
        group="software",
        direction="expense",
        # GAP: AU CoA has no dedicated software-subscription line.
        default_account_code="6-2300",  # Office Expenses
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_TELCO",
        label="Phone & internet",
        group="telco",
        direction="expense",
        default_account_code="6-2800",  # Telephone
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_SUPER",
        label="Personal super contributions",
        group="super",
        direction="expense",
        default_account_code="6-2420",  # Superannuation (expense)
        gst_default=Decimal("0"),
        tax_code="FRE",
        reporting_type="gst_free",
        hint_text=(
            "GST-free. Lodge a notice of intent to claim with your "
            "fund before the BAS due date if you want the deduction."
        ),
    ),
    CashbookCategory(
        code="EXP_TRAINING",
        label="Training & courses",
        group="training",
        direction="expense",
        # GAP: no training line in AU CoA.
        default_account_code="6-2450",  # Other Employer Expenses
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_TOOLS",
        label="Tools (under $300)",
        group="tools",
        direction="expense",
        default_account_code="6-2110",  # Machinery & Equipment
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
        hint_text=(
            "Under $300 = immediate deduction. Over $300 use Capital "
            "purchase instead."
        ),
    ),
    CashbookCategory(
        code="EXP_TRAVEL",
        label="Travel",
        group="travel",
        direction="expense",
        default_account_code="6-3110",  # Travel
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
    ),
    CashbookCategory(
        code="EXP_BANK",
        label="Bank fees",
        group="bank",
        direction="expense",
        # GAP: AU CoA has no bank-fees line; piggyback on "Other Interest".
        default_account_code="6-1930",
        gst_default=Decimal("0"),
        tax_code="INP",
        reporting_type="input_taxed",
        hint_text="GST-free.",
    ),
    CashbookCategory(
        code="EXP_OTHER",
        label="Other expense",
        group="other_expense",
        direction="expense",
        default_account_code="6-2300",  # Office Expenses
        gst_default=Decimal("0.10"),
        tax_code="GST",
        reporting_type="taxable",
    ),
    # ---------- Capital / Personal / Transfer ----------
    CashbookCategory(
        code="CAP_PURCHASE",
        label="Capital purchase (>$300)",
        group="capital",
        direction="expense",
        default_account_code="1-3140",  # Manufacturing Plant at Cost
        gst_default=Decimal("0.10"),
        tax_code="CAP",
        reporting_type="capital",
        hint_text=(
            "Capital asset — depreciation rules apply. Add to asset "
            "register on full edition."
        ),
    ),
    CashbookCategory(
        code="PER_DRAWINGS",
        label="Drawings (personal use)",
        group="personal",
        direction="expense",
        default_account_code="3-1200",  # Capital Drawings
        gst_default=Decimal("0"),
        tax_code=None,
        reporting_type="out_of_scope",
        hint_text=(
            "Not deductible. Money you took for personal use; flagged "
            "in BAS prep."
        ),
    ),
    CashbookCategory(
        code="TX_TRANSFER",
        label="Transfer between accounts",
        group="transfer",
        direction="transfer",
        default_account_code=None,  # routes to a second bank account
        gst_default=Decimal("0"),
        tax_code=None,
        reporting_type="out_of_scope",
        hint_text=(
            "P&L-neutral. Moves money between two of your bank "
            "accounts; not income or expense."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Per-jurisdiction cashbook profiles (registration inversion, Job C shape).
#
# The cashbook was AU-only in v1; the taxonomy above is the signed-off AU
# contract (Gate 1) and STAYS here unchanged. Other jurisdictions register a
# profile from their ``saebooks.jurisdictions.<cc>`` package (see
# ``jurisdictions/ee/cashbook.py``) — the core never imports a jurisdiction.
# A jurisdiction with no registered profile has NO cashbook (typed error at
# the service layer), which preserves the v1 behaviour for everything that
# isn't AU. Relocating the AU table itself into ``jurisdictions/au/`` is a
# follow-up (contract file; byte-identical guardrail applies).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CashbookJurisdictionProfile:
    """One jurisdiction's cashbook wiring: home currency + picker taxonomy."""

    jurisdiction: str
    currency: str
    categories: tuple[CashbookCategory, ...]

    @property
    def by_code(self) -> dict[str, CashbookCategory]:
        return {c.code: c for c in self.categories}


_PROFILES: dict[str, CashbookJurisdictionProfile] = {}


def register_cashbook_profile(profile: CashbookJurisdictionProfile) -> None:
    """Register a jurisdiction's cashbook profile (called by jurisdiction
    packages at import time; re-registration overwrites, idempotent)."""
    _PROFILES[profile.jurisdiction] = profile


register_cashbook_profile(
    CashbookJurisdictionProfile(
        jurisdiction="AU", currency="AUD", categories=DEFAULT_CATEGORIES
    )
)


# ---------------------------------------------------------------------------
# Neutral-core fallback taxonomy (0 jurisdiction modules).
#
# The engine must FUNCTION with no jurisdiction module loaded for a company's
# jurisdiction (architecture directive: bare, jurisdiction-neutral core; the
# model always functions regardless of which/how many modules are bolted on).
# A company whose ``jurisdiction`` has no registered cashbook profile therefore
# gets this MINIMAL, tax-free picker instead of a hard error — the cashbook
# runs in the company's OWN base currency (see ``cashbook._resolve_company``)
# and posts plain two-line entries with NO tax lines.
#
# DESIGN DECISION (flag for product sign-off): the neutral core has no standard
# chart of accounts, so these categories carry ``default_account_code=None``.
# They resolve ONLY through a per-company account_id override
# (``companies.cashbook_categories.overrides[CODE].account_id``). Until a chart
# is mapped, recording an entry raises ``cashbook_account_unresolved`` — the
# "seed/map my chart" onboarding gap (onboarding UI is a consumer-lane
# feature, deliberately out of scope here). All entries are tax-free
# (``tax_code=None``, ``gst_default=0``); a jurisdiction that later ships a
# cashbook module with real tax mapping overrides this fallback entirely.
# ---------------------------------------------------------------------------
NEUTRAL_CATEGORIES: tuple[CashbookCategory, ...] = (
    CashbookCategory(
        code="INC_SALES",
        label="Sales",
        group="income",
        direction="income",
        default_account_code=None,
        gst_default=Decimal("0"),
        tax_code=None,
        reporting_type="out_of_scope",
        hint_text="Money received from customers.",
    ),
    CashbookCategory(
        code="INC_OTHER",
        label="Other income",
        group="income",
        direction="income",
        default_account_code=None,
        gst_default=Decimal("0"),
        tax_code=None,
        reporting_type="out_of_scope",
        hint_text="Anything that doesn't fit Sales.",
    ),
    CashbookCategory(
        code="EXP_PURCHASES",
        label="Purchases",
        group="materials",
        direction="expense",
        default_account_code=None,
        gst_default=Decimal("0"),
        tax_code=None,
        reporting_type="out_of_scope",
        hint_text="Goods and materials bought for the business.",
    ),
    CashbookCategory(
        code="EXP_OTHER",
        label="Other expense",
        group="other_expense",
        direction="expense",
        default_account_code=None,
        gst_default=Decimal("0"),
        tax_code=None,
        reporting_type="out_of_scope",
        hint_text="Any other business expense.",
    ),
    CashbookCategory(
        code="TX_TRANSFER",
        label="Transfer between accounts",
        group="transfer",
        direction="transfer",
        default_account_code=None,
        gst_default=Decimal("0"),
        tax_code=None,
        reporting_type="out_of_scope",
        hint_text="P&L-neutral. Moves money between two of your bank accounts.",
    ),
)

_NEUTRAL_BY_CODE: dict[str, CashbookCategory] = {
    c.code: c for c in NEUTRAL_CATEGORIES
}


class CashbookUnsupportedJurisdiction(KeyError):
    """Raised when a jurisdiction has no registered cashbook profile."""


def profile_for(jurisdiction: str) -> CashbookJurisdictionProfile:
    """Return the cashbook profile for ``jurisdiction``.

    Lazy ``ensure_loaded()`` guard (Job C shape): packaged jurisdictions
    register on import, so load the enabled set before concluding the
    jurisdiction has no cashbook.
    """
    profile = _PROFILES.get(jurisdiction)
    if profile is None:
        from saebooks.bootstrap.jurisdictions import ensure_loaded

        ensure_loaded()
        profile = _PROFILES.get(jurisdiction)
    if profile is None:
        raise CashbookUnsupportedJurisdiction(
            f"Cashbook is not available for jurisdiction {jurisdiction!r} "
            f"(registered: {sorted(_PROFILES)})"
        )
    return profile


# Index by code for O(1) lookup. Built at import time.
_BY_CODE: dict[str, CashbookCategory] = {c.code: c for c in DEFAULT_CATEGORIES}


class UnknownCashbookCategory(KeyError):
    """Raised when a category code does not exist in defaults or overrides."""


def get_default(code: str, jurisdiction: str = "AU") -> CashbookCategory:
    """Look up a default category by code within a jurisdiction's profile.

    A jurisdiction with no registered cashbook module falls back to the
    neutral tax-free taxonomy (``NEUTRAL_CATEGORIES``) so the core keeps
    functioning with zero jurisdiction modules. Raise on unknown code.
    """
    if jurisdiction == "AU":
        table = _BY_CODE
    else:
        try:
            table = profile_for(jurisdiction).by_code
        except CashbookUnsupportedJurisdiction:
            table = _NEUTRAL_BY_CODE
    try:
        return table[code]
    except KeyError as e:
        raise UnknownCashbookCategory(
            f"Unknown cashbook category code: {code!r}"
        ) from e


def all_defaults(jurisdiction: str = "AU") -> tuple[CashbookCategory, ...]:
    """Return the canonical default list in picker order.

    Falls back to the neutral tax-free taxonomy for a jurisdiction with no
    registered cashbook module (zero-modules core still serves a picker).
    """
    if jurisdiction == "AU":
        return DEFAULT_CATEGORIES
    try:
        return profile_for(jurisdiction).categories
    except CashbookUnsupportedJurisdiction:
        return NEUTRAL_CATEGORIES


def resolve_for_company(
    code: str,
    overrides: dict | None,
    jurisdiction: str = "AU",
) -> CashbookCategory:
    """Return the effective category for a company, applying overrides.

    ``overrides`` is the JSONB blob stored on
    ``companies.cashbook_categories`` (or None for bare defaults).
    Shape::

        {
          "version": 1,
          "overrides": {
            "EXP_VEHICLE":  {"label": "Ute & fuel", "account_id": "..."},
            "INC_INTEREST": {"hidden": true}
          }
        }

    A ``hidden: true`` override raises ``UnknownCashbookCategory`` on
    resolve so the UI/API treat it identically to a never-defined code.
    Account-id overrides are honoured by the cashbook service at JE
    creation time — this resolver only carries the override forward.
    """
    base = get_default(code, jurisdiction)
    if not overrides:
        return base
    table = overrides.get("overrides") if isinstance(overrides, dict) else None
    if not isinstance(table, dict):
        return base
    patch = table.get(code)
    if not isinstance(patch, dict):
        return base
    if patch.get("hidden") is True:
        raise UnknownCashbookCategory(
            f"Cashbook category {code!r} is hidden for this company"
        )
    # Only label is overridden in the dataclass — account_id override is
    # consumed by the cashbook service during JE creation, not here.
    return CashbookCategory(
        code=base.code,
        label=str(patch.get("label", base.label)),
        group=base.group,
        direction=base.direction,
        default_account_code=base.default_account_code,
        gst_default=base.gst_default,
        hint_text=base.hint_text,
        tax_code=base.tax_code,
        reporting_type=base.reporting_type,
    )


def resolve_account_id_override(
    code: str,
    overrides: dict | None,
) -> str | None:
    """Return the per-company account UUID override for ``code``, or None.

    The cashbook service uses this to bypass the
    ``default_account_code`` lookup when a customer has repointed a
    category.
    """
    if not overrides:
        return None
    table = overrides.get("overrides") if isinstance(overrides, dict) else None
    if not isinstance(table, dict):
        return None
    patch = table.get(code)
    if not isinstance(patch, dict):
        return None
    raw = patch.get("account_id")
    return str(raw) if raw else None
