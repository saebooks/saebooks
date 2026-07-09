"""Generic, jurisdiction-parameterised return calculator (M1.5 · T8).

Before this theme, AU BAS box logic (G1/G2/G3/G10/G11/1A/1B) was
hardcoded Python branching in ``services/tax_engine/au.py::bas_report``
and duplicated, semi-independently, in ``api/v1/reports.py``'s
``bas_summary`` endpoint — even though a jurisdiction-keyed reference
table, ``TaxReturnBoxDefinition``, already existed to describe exactly
this and was never read. ``generate_return`` is the data-driven
replacement: it reads box rows for a ``(jurisdiction, return_type)``
pair from the reference DB and aggregates the company ledger according
to each box's declared ``aggregation`` recipe, instead of a bespoke
Python branch per box.

``services/tax_engine/au.py::bas_report`` is now a thin wrapper over
``generate_return`` for AU/BAS — see that module. AU box definitions
live at ``saebooks/seeds/jurisdictions/AU/tax_return_box_definitions.yaml``
(read the header there for the ``aggregation`` grammar).

Resilience — no reference-DB coupling for core reporting
----------------------------------------------------------
The reference DB is optional infrastructure: ``REFERENCE_DATABASE_URL``
is unset in the standard test/CI environment today (only
``REFERENCE_MIGRATION_DATABASE_URL``, used for migrations/seeding, is
set there — see ``docker-compose.test.yml``), and ``saebooks.db``
documents ``ReferenceSession`` returning ``None`` as the expected,
non-fatal shape when it is absent. AU BAS reporting is a foundational,
heavily-tested capability — Richard's MODULARITY directive (2026-07-09)
is explicit that no capability's fault should cascade from an
unrelated/optional module being unavailable. So when the reference DB
is unset OR reachable-but-unseeded for a given jurisdiction/return_type,
``generate_return`` falls back to ``_FALLBACK_BOX_DEFINITIONS`` — an
embedded snapshot of the SAME box rows as the AU seed YAML, kept in
lock-step by inspection (both are tiny and change rarely). The reference
table is the authoritative, preferred source whenever it is configured
and seeded; the fallback exists purely so a missing/cold reference DB
degrades AU BAS reporting to "exactly today's behaviour", never to an
error. ``TaxReturnResult.source`` reports which path served a given call
("reference_db" | "embedded_fallback") for observability/tests.

See docs/multi-jurisdiction.md (M1.5) (theme T8).
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import ReferenceSession
from saebooks.models.account import Account
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.journal_line_tax_component import JournalLineTaxComponent
from saebooks.models.reference.tax_code import RefTaxCode, TaxFamily
from saebooks.models.reference.tax_return_box_definition import TaxReturnBoxDefinition
from saebooks.models.tax_code import TaxCode
from saebooks.services.tax_engine.au import (
    _BAS_INCOME_TYPES as _INCOME_ACCOUNT_TYPES,
)
from saebooks.services.tax_engine.au import (  # aliased import, see comment above
    _BAS_PURCHASE_TYPES as _PURCHASE_ACCOUNT_TYPES,
)

logger = logging.getLogger("saebooks.tax_return_generator")


# ---------------------------------------------------------------------------
# 2-letter tax-engine jurisdiction code <-> 3-letter reference-DB code.
# ---------------------------------------------------------------------------

# ``tax_engine.get_engine()`` and every existing caller (Company.jurisdiction,
# TaxCode.jurisdiction, PostingContext.jurisdiction) key off the 2-letter
# code ("AU"). The reference DB's ``jurisdictions.code`` column is 3-letter
# ("AUS") — see saebooks/seeds/jurisdictions/_global/jurisdictions.yaml.
# No shared mapping utility exists elsewhere yet, so this is the first one;
# extend it as further jurisdictions land (NZ/UK/EE are M1/M2/M3 stubs).
_JURISDICTION_TO_REFERENCE_CODE: dict[str, str] = {
    "AU": "AUS",
    "NZ": "NZL",
    "UK": "GBR",
    "EE": "EST",
}


def _to_reference_jurisdiction(jurisdiction: str) -> str:
    """Map a 2-letter engine jurisdiction code to the reference DB's
    3-letter code. Codes already 3-letter (or otherwise unknown) pass
    through unchanged, so a caller that already resolved the reference
    code can call this idempotently."""
    return _JURISDICTION_TO_REFERENCE_CODE.get(jurisdiction.upper(), jurisdiction)


_REFERENCE_TO_JURISDICTION_CODE: dict[str, str] = {
    v: k for k, v in _JURISDICTION_TO_REFERENCE_CODE.items()
}


def _to_engine_jurisdiction(jurisdiction: str) -> str:
    """Map a reference-DB 3-letter jurisdiction code back to the
    2-letter engine code used to key ``_FALLBACK_BOX_DEFINITIONS``.
    Codes already 2-letter (or otherwise unknown) pass through
    unchanged, so this is idempotent and safe to call on either
    convention — the embedded fallback must be reachable regardless of
    which of the two equivalent codes a caller supplies."""
    return _REFERENCE_TO_JURISDICTION_CODE.get(jurisdiction.upper(), jurisdiction.upper())


# ---------------------------------------------------------------------------
# Box-definition parsing.
# ---------------------------------------------------------------------------

_KIND_TAXABLE = "sum_taxable_for_codes"
_KIND_TAX_AMOUNT = "sum_tax_amount_for_codes"
_KIND_MANUAL = "manual"
_FORMULA_PREFIX = "formula:"

_BUCKET_INCOME = "income"
_BUCKET_PURCHASE = "purchase"

_MOD_INCLUSIVE = "gst_inclusive"
_MOD_EXCLUSIVE = "gst_exclusive"


@dataclass(frozen=True, slots=True)
class _BoxDefRow:
    """Jurisdiction-agnostic snapshot of one ``TaxReturnBoxDefinition``
    row — decoupled from the ORM/session so both a live reference-DB
    fetch and the embedded fallback can produce the same shape."""

    box_code: str
    box_label: str
    aggregation: str
    feeder_tax_codes: list[str]
    display_order: int


@dataclass(frozen=True, slots=True)
class _ParsedBox:
    box_code: str
    box_label: str
    display_order: int
    kind: str  # _KIND_TAXABLE | _KIND_TAX_AMOUNT | _KIND_MANUAL
    bucket: str | None  # _BUCKET_INCOME | _BUCKET_PURCHASE | None (manual)
    inclusive: bool
    feeder_codes: frozenset[str]


def _parse_box_definition(row: _BoxDefRow) -> _ParsedBox:
    """Parse a box's ``aggregation`` string into the generic grammar
    documented in the AU seed YAML header. Raises ``ValueError`` for a
    malformed recipe and ``NotImplementedError`` for ``formula:<expr>``
    (declared in the model's comment as a future aggregation kind — no
    seeded box uses it yet, so it is not built)."""
    agg = row.aggregation
    if agg == _KIND_MANUAL:
        return _ParsedBox(
            row.box_code, row.box_label, row.display_order, _KIND_MANUAL, None, False, frozenset()
        )
    if agg.startswith(_FORMULA_PREFIX):
        raise NotImplementedError(
            f"tax_return_generator: box {row.box_code!r} uses a 'formula:' "
            f"aggregation, which is not implemented yet — no seeded box "
            f"needs it today. aggregation={agg!r}"
        )
    parts = agg.split(":")
    kind = parts[0]
    if kind not in (_KIND_TAXABLE, _KIND_TAX_AMOUNT):
        raise ValueError(
            f"tax_return_generator: box {row.box_code!r} has an unknown "
            f"aggregation kind {kind!r} (aggregation={agg!r})"
        )
    if len(parts) < 2 or parts[1] not in (_BUCKET_INCOME, _BUCKET_PURCHASE):
        raise ValueError(
            f"tax_return_generator: box {row.box_code!r} aggregation {agg!r} "
            f"is missing a valid ':income' or ':purchase' bucket."
        )
    bucket = parts[1]
    inclusive = False
    if kind == _KIND_TAXABLE:
        if len(parts) < 3 or parts[2] not in (_MOD_INCLUSIVE, _MOD_EXCLUSIVE):
            raise ValueError(
                f"tax_return_generator: box {row.box_code!r} aggregation "
                f"{agg!r} (sum_taxable_for_codes) is missing a "
                f"':gst_inclusive' or ':gst_exclusive' modifier."
            )
        inclusive = parts[2] == _MOD_INCLUSIVE
    feeders = frozenset(row.feeder_tax_codes or [])
    return _ParsedBox(row.box_code, row.box_label, row.display_order, kind, bucket, inclusive, feeders)


# ---------------------------------------------------------------------------
# Embedded fallback — used only when the reference DB is unset or
# unseeded for the requested jurisdiction/return_type. Mirrors
# saebooks/seeds/jurisdictions/AU/tax_return_box_definitions.yaml exactly;
# see the module docstring ("Resilience") for why this exists.
# ---------------------------------------------------------------------------

_FALLBACK_BOX_DEFINITIONS: dict[tuple[str, str], tuple[_BoxDefRow, ...]] = {
    ("AU", "BAS"): (
        _BoxDefRow(
            "G1", "Total sales (including any GST)",
            "sum_taxable_for_codes:income:gst_inclusive", ["taxable"], 1,
        ),
        _BoxDefRow(
            "G2", "Export sales",
            "sum_taxable_for_codes:income:gst_exclusive", ["export"], 2,
        ),
        _BoxDefRow(
            "G3", "Other GST-free sales",
            "sum_taxable_for_codes:income:gst_exclusive", ["gst_free"], 3,
        ),
        _BoxDefRow(
            "G10", "Capital purchases (including any GST)",
            "sum_taxable_for_codes:purchase:gst_inclusive", ["capital"], 4,
        ),
        _BoxDefRow(
            "G11", "Non-capital purchases (including any GST)",
            "sum_taxable_for_codes:purchase:gst_inclusive", ["taxable"], 5,
        ),
        _BoxDefRow(
            "1A", "GST collected on sales",
            "sum_tax_amount_for_codes:income", ["taxable"], 6,
        ),
        _BoxDefRow(
            "1B", "GST paid on purchases",
            "sum_tax_amount_for_codes:purchase", ["taxable", "capital"], 7,
        ),
    ),
}


async def _fetch_box_definitions(
    jurisdiction: str, return_type: str
) -> tuple[list[_BoxDefRow], str]:
    """Return ``(box_rows, source)`` where ``source`` is
    ``"reference_db"`` or ``"embedded_fallback"``. Reference DB is tried
    first when configured; the embedded fallback covers both "not
    configured" and "configured but not seeded for this jurisdiction/
    return_type" (a real DB error once connected is NOT swallowed — only
    the documented, expected-absence case is).
    """
    ref_code = _to_reference_jurisdiction(jurisdiction)
    if ReferenceSession is not None:
        async with ReferenceSession() as ref:
            result = await ref.execute(
                select(TaxReturnBoxDefinition)
                .where(
                    TaxReturnBoxDefinition.jurisdiction == ref_code,
                    TaxReturnBoxDefinition.return_type == return_type,
                )
                .order_by(TaxReturnBoxDefinition.display_order)
            )
            rows = result.scalars().all()
        if rows:
            return (
                [
                    _BoxDefRow(
                        box_code=r.box_code,
                        box_label=r.box_label,
                        aggregation=r.aggregation,
                        feeder_tax_codes=list(r.feeder_tax_codes or []),
                        display_order=r.display_order,
                    )
                    for r in rows
                ],
                "reference_db",
            )
        logger.warning(
            "tax_return_generator: reference DB configured but no "
            "TaxReturnBoxDefinition rows for %s/%s — using the embedded "
            "fallback box set.", ref_code, return_type,
        )

    fallback = _FALLBACK_BOX_DEFINITIONS.get(
        (_to_engine_jurisdiction(jurisdiction), return_type)
    )
    if fallback is None:
        raise ValueError(
            f"tax_return_generator: no box definitions available for "
            f"jurisdiction={jurisdiction!r} return_type={return_type!r} — "
            f"reference DB unavailable/unseeded and no embedded fallback "
            f"exists for this jurisdiction/return_type."
        )
    return list(fallback), "embedded_fallback"


# ---------------------------------------------------------------------------
# Ledger aggregation.
# ---------------------------------------------------------------------------


async def _aggregate_ledger_by_box(
    session: AsyncSession,
    parsed_boxes: list[_ParsedBox],
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    from_date: date | None,
    to_date: date | None,
    statuses: Sequence[EntryStatus],
    exclude_archived: bool,
) -> dict[str, Decimal]:
    """Aggregate POSTED (or ``statuses``) JournalLine rows into box
    totals per the parsed aggregation recipes.

    One query drives every box: (Account.account_type, TaxCode.
    reporting_type, JournalLine.debit, JournalLine.credit, tax_amount)
    where ``tax_amount`` prefers the summed ``journal_line_tax_components``
    row(s) for the line (M1.5 · T2) and falls back to the legacy
    ``JournalLine.gst_amount`` scalar when no component exists (e.g. a
    line posted before T2 shipped, or via a path that predates
    ``_apply_tax_treatment``).
    """
    amounts: dict[str, Decimal] = {b.box_code: Decimal("0") for b in parsed_boxes}

    ledger_boxes = [b for b in parsed_boxes if b.kind in (_KIND_TAXABLE, _KIND_TAX_AMOUNT)]
    if not ledger_boxes:
        return amounts

    # (bucket, reporting_type) -> boxes that box's row should feed, split
    # by whether the box wants the "net" (taxable-base) or "tax" amount.
    net_index: dict[tuple[str, str], list[_ParsedBox]] = defaultdict(list)
    tax_index: dict[tuple[str, str], list[_ParsedBox]] = defaultdict(list)
    for box in ledger_boxes:
        target = net_index if box.kind == _KIND_TAXABLE else tax_index
        for code in box.feeder_codes:
            target[(box.bucket, code)].append(box)

    # Sum of tax-component rows per line — 1:many-ready (T2), though the
    # AU engine snapshots one component per line today.
    component_totals = (
        select(
            JournalLineTaxComponent.journal_line_id.label("journal_line_id"),
            func.sum(JournalLineTaxComponent.tax_amount).label("component_tax"),
        )
        .group_by(JournalLineTaxComponent.journal_line_id)
        .subquery()
    )

    conditions = [
        JournalEntry.company_id == company_id,
        JournalEntry.status.in_(statuses),
    ]
    if tenant_id is not None:
        conditions.append(JournalEntry.tenant_id == tenant_id)
    if exclude_archived:
        conditions.append(JournalEntry.archived_at.is_(None))
    if from_date:
        conditions.append(JournalEntry.entry_date >= from_date)
    if to_date:
        conditions.append(JournalEntry.entry_date <= to_date)

    stmt = (
        select(
            Account.account_type,
            TaxCode.reporting_type,
            JournalLine.debit,
            JournalLine.credit,
            func.coalesce(
                component_totals.c.component_tax,
                JournalLine.gst_amount,
                0,
            ).label("tax_amount"),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .outerjoin(TaxCode, JournalLine.tax_code_id == TaxCode.id)
        .outerjoin(component_totals, component_totals.c.journal_line_id == JournalLine.id)
        .where(and_(*conditions))
    )

    result = await session.execute(stmt)
    for acct_type, reporting_type, debit, credit, tax_amount in result.all():
        rt = reporting_type or ""
        debit = debit or Decimal("0")
        credit = credit or Decimal("0")
        tax_amount = Decimal(tax_amount or 0)

        if acct_type in _INCOME_ACCOUNT_TYPES:
            bucket = _BUCKET_INCOME
            net = credit - debit
        elif acct_type in _PURCHASE_ACCOUNT_TYPES:
            bucket = _BUCKET_PURCHASE
            net = debit - credit
        else:
            continue

        for box in net_index.get((bucket, rt), ()):
            amounts[box.box_code] += (net + tax_amount) if box.inclusive else net
        for box in tax_index.get((bucket, rt), ()):
            amounts[box.box_code] += tax_amount

    return amounts


# ---------------------------------------------------------------------------
# Public results + entry points.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaxReturnBoxResult:
    box_code: str
    box_label: str
    amount: Decimal
    display_order: int


@dataclass(frozen=True, slots=True)
class TaxReturnResult:
    jurisdiction: str
    return_type: str
    period_from: date | None
    period_to: date | None
    boxes: dict[str, TaxReturnBoxResult]
    source: str  # "reference_db" | "embedded_fallback"

    def amount(self, box_code: str) -> Decimal:
        box = self.boxes.get(box_code)
        return box.amount if box is not None else Decimal("0")


async def generate_return(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    jurisdiction: str,
    return_type: str,
    from_date: date | None = None,
    to_date: date | None = None,
    tenant_id: uuid.UUID | None = None,
    statuses: Sequence[EntryStatus] = (EntryStatus.POSTED,),
    exclude_archived: bool = False,
) -> TaxReturnResult:
    """Build a return for ``jurisdiction``/``return_type`` over
    ``[from_date, to_date]`` by reading box definitions from the
    reference DB (``TaxReturnBoxDefinition``, falling back to an
    embedded snapshot — see module docstring) and aggregating
    ``company_id``'s ledger against each box's recipe.

    ``session`` is the COMPANY-DB session (ledger data); the reference
    DB is queried on its own separate ``ReferenceSession`` internally —
    the two databases are never joined at the SQL level (see
    ``saebooks/db.py``'s "Reference DB" section and
    ``tests/integration/test_cross_db_join.py``).

    ``tenant_id``/``statuses``/``exclude_archived`` default to
    ``services.tax_engine.au.bas_report``'s existing filter shape
    (no tenant filter, POSTED only, archived included) so the AU thin
    wrapper reproduces its pre-T8 numbers exactly. Callers with a
    stricter scope (e.g. ``api/v1/reports.py``, which also filters by
    tenant and excludes archived entries) pass their own values.
    """
    box_rows, source = await _fetch_box_definitions(jurisdiction, return_type)
    parsed = [_parse_box_definition(r) for r in box_rows]
    amounts = await _aggregate_ledger_by_box(
        session,
        parsed,
        company_id=company_id,
        tenant_id=tenant_id,
        from_date=from_date,
        to_date=to_date,
        statuses=statuses,
        exclude_archived=exclude_archived,
    )
    boxes = {
        b.box_code: TaxReturnBoxResult(
            box_code=b.box_code,
            box_label=b.box_label,
            amount=amounts.get(b.box_code, Decimal("0")),
            display_order=b.display_order,
        )
        for b in parsed
    }
    return TaxReturnResult(
        jurisdiction=jurisdiction,
        return_type=return_type,
        period_from=from_date,
        period_to=to_date,
        boxes=boxes,
        source=source,
    )


async def aggregate_return_boxes(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    jurisdiction: str,
    return_type: str,
    from_date: date | None = None,
    to_date: date | None = None,
    tenant_id: uuid.UUID | None = None,
    statuses: Sequence[EntryStatus] = (EntryStatus.POSTED,),
    exclude_archived: bool = False,
) -> dict[str, Decimal]:
    """Figures-only counterpart to ``generate_return`` — box_code ->
    aggregated Decimal, without the reference-DB label/order wrapping.

    For callers that only want a subset of a jurisdiction's boxes
    computed the SAME data-driven way (e.g. ``api/v1/reports.py``'s
    ``bas_summary``, which reuses this for G2/G3/G10 but keeps its own
    bespoke G1/G11/1A/1B — see ``_bas_aggregate``'s docstring there for
    why those four are NOT delegated here).
    """
    box_rows, _source = await _fetch_box_definitions(jurisdiction, return_type)
    parsed = [_parse_box_definition(r) for r in box_rows]
    return await _aggregate_ledger_by_box(
        session,
        parsed,
        company_id=company_id,
        tenant_id=tenant_id,
        from_date=from_date,
        to_date=to_date,
        statuses=statuses,
        exclude_archived=exclude_archived,
    )


# ---------------------------------------------------------------------------
# RefTaxCode-sourced rate helpers — replace the old hardcoded
# api/v1/reports.py::_GST_RATE / _GST_INCLUSIVE_FRACTION constants (dead
# code as of T8: nothing in that module actually read them — 1A/1B are
# sourced from the GST control-account balances, not a rate multiplication;
# see reports.py::_bas_gst_amounts's "Round-2 audit fix #6" docstring).
# Kept generic/jurisdiction-neutral for any future caller that legitimately
# needs an inclusive fraction (e.g. a margin-scheme estimate, a quote
# calculator) rather than the ledger-derived control-account totals.
# ---------------------------------------------------------------------------


async def indirect_tax_rate_percent(
    jurisdiction: str, *, tax_family: str = TaxFamily.VAT_GST
) -> Decimal:
    """Standard sale-side VAT/GST rate for ``jurisdiction``, as
    percentage points (``Decimal("10.0000")`` for AU, not ``0.10``) —
    the highest-rate active ``sale``/``both`` direction ``RefTaxCode``
    row of ``tax_family`` for the jurisdiction (the jurisdiction's
    "standard rate"; concessional/zero rates are separate codes).

    Raises ``ReferenceNotConfiguredError`` (propagated from
    ``saebooks.db``) if the reference DB is not configured — unlike
    ``generate_return``, this helper has no embedded fallback: nothing
    on the core BAS-reporting path calls it (see module note above), so
    there is no "must never fail" requirement to protect here, and a
    silently-wrong hardcoded rate is worse than a loud error.
    """
    from saebooks.db import ReferenceNotConfiguredError

    if ReferenceSession is None:
        raise ReferenceNotConfiguredError(
            "REFERENCE_DATABASE_URL is not configured; "
            "indirect_tax_rate_percent has no fallback."
        )
    ref_code = _to_reference_jurisdiction(jurisdiction)
    async with ReferenceSession() as ref:
        result = await ref.execute(
            select(RefTaxCode)
            .where(
                RefTaxCode.jurisdiction == ref_code,
                RefTaxCode.tax_family == tax_family,
                RefTaxCode.direction.in_(("sale", "both")),
                RefTaxCode.rate_percent > 0,
            )
            .order_by(RefTaxCode.rate_percent.desc())
            .limit(1)
        )
        row = result.scalars().first()
    if row is None:
        raise ValueError(
            f"tax_return_generator: no active sale-direction {tax_family!r} "
            f"RefTaxCode found for jurisdiction {ref_code!r}."
        )
    return row.rate_percent


async def indirect_tax_inclusive_fraction(
    jurisdiction: str, *, tax_family: str = TaxFamily.VAT_GST
) -> Decimal:
    """Generic tax-inclusive fraction: ``rate / (100 + rate)`` where
    ``rate`` is the RefTaxCode-sourced percentage (AU GST 10 -> 1/11,
    matching the old hardcoded ``_GST_INCLUSIVE_FRACTION = 1/11``, but
    now derived from the reference table for any jurisdiction/rate
    instead of a hardcoded AU-only constant).
    """
    rate_percent = await indirect_tax_rate_percent(jurisdiction, tax_family=tax_family)
    return rate_percent / (Decimal("100") + rate_percent)
