"""OSS-Q (Union One-Stop-Shop quarterly VAT return) — compute layer.

EE-frontier build plan, Module 2. GENERATOR + RATE SEED ONLY — the
wire-format serializer is a deliberate STOP, see ``serializer.py``.

Corrects the original task premise (see
``~/records/saebooks/ee-frontier-build-plan.md`` §"MODULE 2"): OSS-Q boxes
were NOT already seeded (``tax_return_box_definitions.yaml`` carried a
single deliberate STUB row) and the per-member-state OSS rate table did
not exist. Both are added by this module's sibling changes
(``alembic_reference/versions/0011_oss_member_state_rates.py`` +
``EE/oss_member_state_rates.yaml`` + the OSS-Q section of
``tax_return_box_definitions.yaml``).

**Parallel to, not built on, the KMD box engine** — same posture as
``kmd_inf.generator`` (see that module's own docstring): OSS-Q is a
per-(member state of consumption x VAT rate) repeating listing, not a
fixed box vector. ``tax_return_generator.generate_return`` assumes a
static, enumerable box set per (jurisdiction, return_type) — it has no
shape for "one cell per member state a company actually sold into this
period", which varies company-to-company and period-to-period. This
module is therefore a self-contained sibling, exactly like
``kmd_inf``/``tsd``, not a caller of the box engine.

Compute shape
--------------
1. Walk POSTED ``Invoice`` rows (Union OSS is B2C SALES only — no bills,
   no purchase side) whose lines carry a company-side ``TaxCode`` tagged
   ``mapping.OSS_REPORTING_TYPE`` ("oss_eu_b2c").
2. Resolve each invoice's destination member state from its
   ``Contact.country`` free-text field via ``mapping.normalize_member_state``
   — an unrecognised country text is a data-quality error (line excluded,
   never silently dropped or guessed).
3. Resolve the VAT rate to apply: the line's own ``TaxCode.rate`` if set
   (>0 — an explicit per-line override, e.g. a reduced-rated OSS supply),
   else the destination member state's STANDARD rate from
   ``oss_member_state_rates`` (reference DB, with an embedded fallback —
   mirrors ``tax_return_generator._fetch_box_definitions``'s
   reference-DB-optional discipline, since ``REFERENCE_DATABASE_URL`` is
   unset in the standard test/CI environment). No rate resolvable is
   also a data-quality error, never a silent 0.
4. Aggregate into one cell per (member_state_code, vat_rate_percent) —
   ``aggregate_oss_cells``, a PURE function over already-resolved
   ``OssSaleLine`` facts, independently unit-testable with no DB.

Not covered by this pass (named gaps, not silently dropped)
--------------------------------------------------------------
* **IOSS** (import scheme, ``IOSS_IMPORT`` marker code, monthly not
  quarterly) — the build plan's Module 2 scope is OSS-Q specifically;
  IOSS is a separate return with its own period cadence and threshold
  rules. Out of scope for this pass.
* **Reduced/parking rates beyond a company's explicit override** —
  ``oss_member_state_rates`` only carries each state's STANDARD rate
  (0011's migration docstring). A company selling a reduced-rated good/
  service under OSS must provision its own rate-carrying TaxCode (see
  ``mapping.py``'s header) — this module never guesses a reduced rate.
* **Credit notes** — Union OSS supports negative corrections, but this
  pass reads posted ``Invoice`` rows only (mirrors kmd_inf's own Part-A-
  only-has-credit-notes scope, simplified further here since the task
  brief bounds this module to "generator + rate seed"). A future packet
  should extend this the same way ``kmd_inf.generator`` handles
  ``CreditNote`` rows, signed negative.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from saebooks.db import ReferenceSession
from saebooks.models.company import Company
from saebooks.models.contact import Contact
from saebooks.models.invoice import Invoice, InvoiceStatus
from saebooks.models.tax_code import TaxCode
from saebooks.services.lodgement.oss_q.mapping import (
    MEMBER_STATE_NAMES,
    OSS_REPORTING_TYPE,
    alpha3_to_alpha2,
    normalize_member_state,
)

_ZERO = Decimal("0")
_TWOPLACES = Decimal("0.01")


def _q2(value: Decimal) -> Decimal:
    """Quantize to the cent, half-up — same convention as
    ``kmd_inf.generator._q2`` / ``kmd.serializer._money``."""
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


class OssQCompanyConfigError(ValueError):
    """Company not configured to generate OSS-Q — mirrors
    ``kmd_inf.generator.KmdInfCompanyConfigError``. Every OSS-Q figure is
    computed and reported in EUR (the Union OSS scheme's own currency);
    a non-EUR ``Company.base_currency`` would silently mislabel a
    wrong-currency figure as EUR."""


# ---------------------------------------------------------------------------
# Embedded fallback — standard VAT rates, kept in lock-step BY INSPECTION
# with EE/oss_member_state_rates.yaml (same discipline as
# tax_return_generator._FALLBACK_BOX_DEFINITIONS and kmd_inf.generator's
# own comments on why this can't be resolved at runtime when
# REFERENCE_DATABASE_URL is unset — the standard test/CI environment).
# ---------------------------------------------------------------------------
_EMBEDDED_STANDARD_RATES: dict[str, Decimal] = {
    "DE": Decimal("19.0000"),
    "FR": Decimal("20.0000"),
    "IT": Decimal("22.0000"),
    "ES": Decimal("21.0000"),
    "PT": Decimal("23.0000"),
    "NL": Decimal("21.0000"),
    "BE": Decimal("21.0000"),
    "LU": Decimal("17.0000"),
    "AT": Decimal("20.0000"),
    "IE": Decimal("23.0000"),
    "FI": Decimal("25.5000"),
    "SE": Decimal("25.0000"),
    "DK": Decimal("25.0000"),
    "PL": Decimal("23.0000"),
    "CZ": Decimal("21.0000"),
    "LV": Decimal("21.0000"),
    "LT": Decimal("21.0000"),
}


async def _fetch_member_state_rates() -> dict[str, Decimal]:
    """Return {alpha-2 code: standard_vat_rate_percent}. Reference DB is
    tried first when configured; the embedded fallback covers both
    "not configured" and "configured but unseeded" (mirrors
    ``tax_return_generator._fetch_box_definitions``'s two-case fallback
    posture — see that function's docstring)."""
    if ReferenceSession is not None:
        from saebooks.models.reference.oss_member_state_rate import (
            OssMemberStateRate,
        )

        async with ReferenceSession() as ref:
            result = await ref.execute(select(OssMemberStateRate))
            rows = result.scalars().all()
        if rows:
            rates: dict[str, Decimal] = {}
            for r in rows:
                a2 = alpha3_to_alpha2(r.country_code)
                if a2 is not None:
                    rates[a2] = r.standard_vat_rate_percent
            if rates:
                return rates
    return dict(_EMBEDDED_STANDARD_RATES)


# ---------------------------------------------------------------------------
# Pure aggregation — no DB. This is the function the task's TESTS section
# asks to be exercised standalone.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OssSaleLine:
    """One already-resolved OSS-reportable sale fact — the pure
    aggregation function's input contract. ``document_id``/``document_number``
    are provenance-only (not used for grouping)."""

    member_state_code: str  # ISO 3166-1 alpha-2, e.g. "DE"
    taxable_base: Decimal  # EUR, ex-VAT
    vat_rate_percent: Decimal  # percentage points, e.g. 19.0000
    document_id: uuid.UUID | None = None
    document_number: str | None = None


@dataclass(frozen=True)
class OssQCell:
    """One (member state, rate) cell of the OSS-Q member-state breakdown
    (``tax_return_box_definitions.yaml``'s ``MS_BREAKDOWN`` box)."""

    member_state_code: str
    member_state_name: str
    vat_rate_percent: Decimal
    taxable_base: Decimal
    vat_amount: Decimal


@dataclass(frozen=True)
class OssQDataQualityError:
    kind: str  # "unmapped_country" | "no_rate" | "unresolved_tax_code"
    message: str
    document_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None


@dataclass
class OssQListing:
    company_id: uuid.UUID
    period_start: date
    period_end: date
    cells: list[OssQCell] = field(default_factory=list)
    errors: list[OssQDataQualityError] = field(default_factory=list)

    def total_vat_payable(self) -> Decimal:
        return sum((c.vat_amount for c in self.cells), _ZERO)

    def total_taxable_base(self) -> Decimal:
        return sum((c.taxable_base for c in self.cells), _ZERO)


def aggregate_oss_cells(
    lines: Sequence[OssSaleLine],
    *,
    member_state_names: dict[str, str] = MEMBER_STATE_NAMES,
) -> list[OssQCell]:
    """Group already-resolved sale facts into one cell per (member state,
    rate), summing the taxable base and deriving the VAT amount from the
    SUMMED base (cell-level rounding, not a sum of independently-rounded
    per-line amounts — same "round per box after aggregation" convention
    ``tax_return_generator._evaluate_formula_boxes`` documents for KMD).

    UNVERIFIED rounding basis: cell-level (sum bases, THEN round, THEN
    apply rate) vs. per-supply-then-summed is a design choice made here
    by analogy to the KMD convention above, not sourced from the OSS
    Union-scheme Implementing Regulation — same class of open question as
    ``tax_return_generator._FORMULA_ROUNDING``'s own ROUND_HALF_UP
    tie-break flag. Confirm against the real Commission guidance before
    this feeds a filed figure.

    Pure function — no DB, no I/O. Deterministic output order: member
    state code, then rate, ascending (stable for golden/snapshot tests).
    """
    totals: dict[tuple[str, Decimal], Decimal] = {}
    for line in lines:
        key = (line.member_state_code, line.vat_rate_percent)
        totals[key] = totals.get(key, _ZERO) + line.taxable_base

    cells: list[OssQCell] = []
    for (ms_code, rate), base in sorted(totals.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        base_q = _q2(base)
        vat_amount = _q2(base_q * rate / Decimal("100"))
        cells.append(
            OssQCell(
                member_state_code=ms_code,
                member_state_name=member_state_names.get(ms_code, ms_code),
                vat_rate_percent=rate,
                taxable_base=base_q,
                vat_amount=vat_amount,
            )
        )
    return cells


# ---------------------------------------------------------------------------
# Async DB walk — posted Invoice rows -> OssSaleLine facts -> aggregate.
# ---------------------------------------------------------------------------


async def generate_oss_q(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> OssQListing:
    """Assemble the OSS-Q member-state breakdown for one quarter from
    posted invoices whose lines carry a ``mapping.OSS_REPORTING_TYPE``
    tax code. See module docstring for the full compute shape and named
    gaps (IOSS, reduced-rate guessing, credit notes — all explicitly out
    of scope for this pass, not silently dropped)."""
    company = await session.get(Company, company_id)
    if company is None:
        raise OssQCompanyConfigError(f"Company {company_id} not found")
    if company.base_currency != "EUR":
        raise OssQCompanyConfigError(
            f"Company {company_id} has base_currency={company.base_currency!r}, "
            "not 'EUR' — OSS-Q's taxable-base/VAT figures are computed "
            "directly from ledger base-currency amounts with no "
            "conversion. Set Company.base_currency='EUR' before "
            "generating OSS-Q for this company."
        )

    rates = await _fetch_member_state_rates()

    # Period basis mirrors kmd_inf's Part A convention: settlement_date
    # when set, else issue_date.
    period_basis_date = func.coalesce(Invoice.settlement_date, Invoice.issue_date)
    result = await session.execute(
        select(Invoice)
        .options(selectinload(Invoice.lines))
        .where(
            Invoice.company_id == company_id,
            Invoice.status == InvoiceStatus.POSTED,
            period_basis_date >= period_start,
            period_basis_date <= period_end,
        )
        .order_by(period_basis_date, Invoice.number)
    )
    invoices = list(result.scalars().all())

    tax_code_ids = {
        ln.tax_code_id
        for inv in invoices
        for ln in inv.lines
        if ln.tax_code_id
    }
    contact_ids = {inv.contact_id for inv in invoices if inv.contact_id}

    tax_codes: dict[uuid.UUID, TaxCode] = {}
    if tax_code_ids:
        tc_result = await session.execute(select(TaxCode).where(TaxCode.id.in_(tax_code_ids)))
        tax_codes = {tc.id: tc for tc in tc_result.scalars().all()}

    contacts: dict[uuid.UUID, Contact] = {}
    if contact_ids:
        c_result = await session.execute(select(Contact).where(Contact.id.in_(contact_ids)))
        contacts = {c.id: c for c in c_result.scalars().all()}

    sale_lines: list[OssSaleLine] = []
    errors: list[OssQDataQualityError] = []

    for inv in invoices:
        oss_lines = [
            ln for ln in inv.lines
            if ln.tax_code_id
            and (tc := tax_codes.get(ln.tax_code_id)) is not None
            and tc.reporting_type == OSS_REPORTING_TYPE
        ]
        if not oss_lines:
            continue

        contact = contacts.get(inv.contact_id) if inv.contact_id else None
        country_text = contact.country if contact else None
        ms_code = normalize_member_state(country_text)
        if ms_code is None:
            errors.append(
                OssQDataQualityError(
                    kind="unmapped_country",
                    message=(
                        f"Invoice {inv.number!r} has OSS-reportable lines but its "
                        f"contact's country ({country_text!r}) does not resolve to "
                        "a recognised OSS destination member state — see "
                        "mapping.normalize_member_state. Line(s) excluded."
                    ),
                    document_id=inv.id,
                    contact_id=inv.contact_id,
                )
            )
            continue

        fx_rate = Decimal(str(inv.fx_rate or Decimal("1")))
        # Group by the effective rate on this invoice's OSS lines — most
        # invoices carry one rate; a rare mixed-rate invoice (company
        # provisioned two OSS TaxCode overrides) still nets out correctly
        # since aggregate_oss_cells groups by (member_state, rate) anyway.
        by_rate: dict[Decimal, Decimal] = {}
        for ln in oss_lines:
            tc = tax_codes[ln.tax_code_id]
            rate = tc.rate if tc.rate and tc.rate > 0 else rates.get(ms_code)
            if rate is None:
                errors.append(
                    OssQDataQualityError(
                        kind="no_rate",
                        message=(
                            f"Invoice {inv.number!r} sells into member state "
                            f"{ms_code!r} but no standard rate is seeded/embedded "
                            "for it and the tax code carries no override rate. "
                            "Line excluded."
                        ),
                        document_id=inv.id,
                        contact_id=inv.contact_id,
                    )
                )
                continue
            base = _q2(ln.line_subtotal * fx_rate)
            by_rate[rate] = by_rate.get(rate, _ZERO) + base

        for rate, base in by_rate.items():
            sale_lines.append(
                OssSaleLine(
                    member_state_code=ms_code,
                    taxable_base=base,
                    vat_rate_percent=rate,
                    document_id=inv.id,
                    document_number=inv.number,
                )
            )

    cells = aggregate_oss_cells(sale_lines)
    return OssQListing(
        company_id=company_id, period_start=period_start, period_end=period_end,
        cells=cells, errors=errors,
    )
