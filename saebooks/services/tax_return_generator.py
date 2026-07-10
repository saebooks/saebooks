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

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T8).
"""
from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.db import ReferenceSession
from saebooks.models.account import Account
from saebooks.models.journal import EntryStatus, JournalEntry, JournalLine
from saebooks.models.journal_line_tax_component import JournalLineTaxComponent
from saebooks.models.reference.tax_code import RefTaxCode, TaxFamily
from saebooks.models.reference.tax_return_box_definition import TaxReturnBoxDefinition
from saebooks.models.tax_code import TaxCode
from saebooks.models.tax_return import TaxReturn, TaxReturnStatus
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
_KIND_FORMULA = "formula"
_FORMULA_PREFIX = "formula:"  # legacy inline form — rejected, see below.

_BUCKET_INCOME = "income"
_BUCKET_PURCHASE = "purchase"
# Role-based buckets (KMD-formula support Packet 3, scope §3.4 point 3) —
# select JournalLineTaxComponent rows by their OWN direction
# ("output"/"input"), not by the owning line's Account.account_type.
# This is what lets a purchase-account reverse-charge line feed an
# output-role box: the line's account is EXPENSE (bucket=purchase) but
# its output-role component's direction is "output", so a box keyed
# ":output:" picks it up regardless of the account bucket. See
# _aggregate_ledger_by_box's second query for the implementation and its
# docstring for why this is a SEPARATE query path rather than folded
# into the existing income/purchase one (component-level, not
# line-level, granularity — a line can carry components of both
# directions at once, which the old per-line-summed subquery cannot
# disambiguate).
_BUCKET_OUTPUT = "output"
_BUCKET_INPUT = "input"
_ACCOUNT_TYPE_BUCKETS = (_BUCKET_INCOME, _BUCKET_PURCHASE)
_ROLE_BUCKETS = (_BUCKET_OUTPUT, _BUCKET_INPUT)

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
    formula: str | None = None


@dataclass(frozen=True, slots=True)
class _ParsedBox:
    box_code: str
    box_label: str
    display_order: int
    kind: str  # _KIND_TAXABLE | _KIND_TAX_AMOUNT | _KIND_MANUAL | _KIND_FORMULA
    bucket: str | None  # _BUCKET_INCOME | _BUCKET_PURCHASE | None (manual/formula)
    inclusive: bool
    feeder_codes: frozenset[str]
    formula: str | None = None  # raw expression, only set when kind == _KIND_FORMULA
    # Rate-aware RC routing (finding 1): for a role bucket ("output" /
    # "input") a box may pin a specific applied rate with an ``@<rate>``
    # qualifier (e.g. ``sum_taxable_for_codes:output@9:gst_exclusive``).
    # None means "match any rate" (the pre-finding-1 behaviour). This is
    # what lets one reverse-charge reporting_type tag fan a 24% base into
    # box 1, a 9% base into box 2 and a 13% base into box 2-2 — the rate,
    # not the tag, discriminates. Only meaningful for role buckets.
    role_rate: Decimal | None = None


def _parse_box_definition(row: _BoxDefRow) -> _ParsedBox:
    """Parse a box's ``aggregation`` string into the generic grammar
    documented in the AU seed YAML header. Raises ``ValueError`` for a
    malformed recipe.

    ``aggregation == "formula"`` is the box-arithmetic kind (KMD-formula
    support Packet 1) — the expression is carried in the dedicated
    ``formula`` column, NOT inlined into ``aggregation`` (box 4's
    rate-formula overflows ``aggregation``'s ``String(64)``). This
    function only records the raw expression string; parsing it into an
    AST and evaluating it happens later, at the return level, once every
    box in the (jurisdiction, return_type) set is known (formula boxes
    can reference each other — see ``_evaluate_formula_boxes``).

    The legacy inline ``formula:<expr>`` prefix (once a reserved,
    unimplemented placeholder) is now actively rejected with a
    ``ValueError`` pointing at the correct dedicated-column form — no
    seeded box ever used it, so this is a safe tightening, not a
    behaviour change for real data.
    """
    agg = row.aggregation
    if agg == _KIND_MANUAL:
        return _ParsedBox(
            row.box_code, row.box_label, row.display_order, _KIND_MANUAL, None, False, frozenset()
        )
    if agg == _KIND_FORMULA:
        expr = (row.formula or "").strip()
        if not expr:
            raise ValueError(
                f"tax_return_generator: box {row.box_code!r} has "
                f"aggregation='formula' but no expression in its 'formula' "
                f"column."
            )
        return _ParsedBox(
            row.box_code, row.box_label, row.display_order, _KIND_FORMULA,
            None, False, frozenset(), expr,
        )
    if agg.startswith(_FORMULA_PREFIX):
        raise ValueError(
            f"tax_return_generator: box {row.box_code!r} uses the legacy "
            f"inline 'formula:<expr>' aggregation form, which is not "
            f"supported. Set aggregation='formula' and carry the "
            f"expression in the dedicated 'formula' column instead "
            f"(aggregation={agg!r})."
        )
    parts = agg.split(":")
    kind = parts[0]
    if kind not in (_KIND_TAXABLE, _KIND_TAX_AMOUNT):
        raise ValueError(
            f"tax_return_generator: box {row.box_code!r} has an unknown "
            f"aggregation kind {kind!r} (aggregation={agg!r})"
        )
    # A role bucket ("output"/"input") may carry an optional "@<rate>"
    # qualifier (finding 1 — rate-aware RC routing), e.g. "output@9".
    bucket_token = parts[1] if len(parts) >= 2 else ""
    role_rate: Decimal | None = None
    base_bucket = bucket_token
    if "@" in bucket_token:
        base_bucket, _, rate_str = bucket_token.partition("@")
        if base_bucket not in (_BUCKET_OUTPUT, _BUCKET_INPUT):
            raise ValueError(
                f"tax_return_generator: box {row.box_code!r} aggregation "
                f"{agg!r} — the '@<rate>' qualifier is only valid on an "
                f"':output' or ':input' role bucket, not {base_bucket!r}."
            )
        try:
            role_rate = Decimal(rate_str)
        except (ArithmeticError, ValueError) as exc:
            raise ValueError(
                f"tax_return_generator: box {row.box_code!r} aggregation "
                f"{agg!r} has an invalid rate qualifier {rate_str!r}."
            ) from exc
    if base_bucket not in (
        _BUCKET_INCOME, _BUCKET_PURCHASE, _BUCKET_OUTPUT, _BUCKET_INPUT,
    ):
        raise ValueError(
            f"tax_return_generator: box {row.box_code!r} aggregation {agg!r} "
            f"is missing a valid ':income', ':purchase', ':output' or "
            f"':input' bucket."
        )
    bucket = base_bucket
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
    return _ParsedBox(
        row.box_code, row.box_label, row.display_order, kind, bucket,
        inclusive, feeders, role_rate=role_rate,
    )


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
                        formula=r.formula,
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

    Two independent query paths, kept SEPARATE on purpose (KMD-formula
    support Packet 3, scope §3.4 point 3 — "bucket-only keying breaks"):

    1. Account-type-keyed boxes (bucket "income"/"purchase", unchanged
       since before Packet 3) — one query: (Account.account_type,
       TaxCode.reporting_type, JournalLine.debit, JournalLine.credit,
       tax_amount) where ``tax_amount`` prefers the SUMMED
       ``journal_line_tax_components`` for the line (M1.5 · T2,
       blending every component's tax together) and falls back to the
       legacy ``JournalLine.gst_amount`` scalar when no component
       exists. This blended-per-line sum is exactly right when a line
       has at most one component (every AU line, every EE line outside
       reverse charge) — it would be WRONG for a reverse-charge line
       (whose two components' tax amounts must stay separate, one
       feeding an output box, one an input box), which is why no
       "income"/"purchase" box's feeder_tax_codes lists a reverse-charge
       reporting_type: the account-type bucket is entirely unaware of
       component role and must never be asked to disambiguate it.
    2. Role-keyed boxes (bucket "output"/"input", new in Packet 3) — a
       SECOND query, one row per JournalLineTaxComponent (not blended
       per line), matched by the component's OWN ``direction`` and the
       owning line's TaxCode.reporting_type. This is what lets a
       reverse-charge purchase-account line's OUTPUT-role component
       reach an output-role box (e.g. EE KMD box 1's RC sub-box) while
       its INPUT-role component, on the very same line, separately
       reaches an input-role box (KMD box 5's RC sub-box) — component-
       level granularity is required precisely because both roles live
       on the same line and must not be summed together.

    A box definition picks EXACTLY ONE bucket (never both) — an EE box
    that needs BOTH a domestic (account-type) contribution and an RC
    (role) contribution is expressed as two separate ledger boxes
    (one per bucket) combined by a ``formula`` box (Packet 1's engine),
    not as one box spanning both query paths. See the EE KMD seed's box
    1/5 comments for the concrete example.
    """
    amounts: dict[str, Decimal] = {b.box_code: Decimal("0") for b in parsed_boxes}

    all_ledger_boxes = [b for b in parsed_boxes if b.kind in (_KIND_TAXABLE, _KIND_TAX_AMOUNT)]
    if not all_ledger_boxes:
        return amounts

    ledger_boxes = [b for b in all_ledger_boxes if b.bucket in _ACCOUNT_TYPE_BUCKETS]
    role_boxes = [b for b in all_ledger_boxes if b.bucket in _ROLE_BUCKETS]

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

    if ledger_boxes:
        # (bucket, reporting_type) -> boxes that box's row should feed,
        # split by whether the box wants the "net" (taxable-base) or
        # "tax" amount.
        net_index: dict[tuple[str, str], list[_ParsedBox]] = defaultdict(list)
        tax_index: dict[tuple[str, str], list[_ParsedBox]] = defaultdict(list)
        for box in ledger_boxes:
            target = net_index if box.kind == _KIND_TAXABLE else tax_index
            for code in box.feeder_codes:
                target[(box.bucket, code)].append(box)

        # Sum of tax-component rows per line — 1:many-ready (T2), blended
        # across every component on the line (safe here: only boxes
        # feeding off "income"/"purchase" read this path, and no such
        # box's feeder list names a reverse-charge reporting_type — see
        # this function's docstring).
        component_totals = (
            select(
                JournalLineTaxComponent.journal_line_id.label("journal_line_id"),
                func.sum(JournalLineTaxComponent.tax_amount).label("component_tax"),
            )
            .group_by(JournalLineTaxComponent.journal_line_id)
            .subquery()
        )

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

            # Finding 3/11 — net the TAX by direction, not just the base.
            # ``net`` already carries the bucket-relative sign (positive on
            # a normal line, negative when the line's debit/credit is
            # reversed), so ``sign(net)`` is the line's orientation: +1 for
            # a normal posting, -1 for its reversal. A reversal entry (both
            # it and the REVERSED original are in REPORTABLE_STATUSES —
            # services.reports) mirrors the original's components onto its
            # swapped lines (services.journal._apply_tax_treatment), so
            # signing the tax the same way the base is signed makes the
            # original (+tax) and the reversal (-tax) cancel — previously
            # the base cancelled but the flat-summed tax did not, leaving a
            # reversed period's tax boxes overstated. Normal (never-
            # reversed) lines have net >= 0, so the sign is +1 and the
            # value is byte-identical to before.
            orient = Decimal(-1) if net < 0 else Decimal(1)
            for box in net_index.get((bucket, rt), ()):
                amounts[box.box_code] += (
                    (net + orient * tax_amount) if box.inclusive else net
                )
            for box in tax_index.get((bucket, rt), ()):
                amounts[box.box_code] += orient * tax_amount

    if role_boxes:
        role_net_index: dict[tuple[str, str], list[_ParsedBox]] = defaultdict(list)
        role_tax_index: dict[tuple[str, str], list[_ParsedBox]] = defaultdict(list)
        for box in role_boxes:
            target = role_net_index if box.kind == _KIND_TAXABLE else role_tax_index
            for code in box.feeder_codes:
                target[(box.bucket, code)].append(box)

        def _rate_matches(box: _ParsedBox, rate_applied: Decimal | None) -> bool:
            # A rate-pinned role box (finding 1 — e.g. box 2_RC at @9)
            # matches only components whose applied rate equals its
            # qualifier; an unpinned box (role_rate is None) matches any
            # rate, exactly as before finding 1.
            if box.role_rate is None:
                return True
            if rate_applied is None:
                return False
            return Decimal(rate_applied) == box.role_rate

        # One row PER COMPONENT (no per-line grouping/sum) — the whole
        # point of this path is to keep a reverse-charge line's two
        # components (different direction, different role) separate.
        # TaxCode.reporting_type is still sourced via the owning LINE's
        # tax_code_id (JournalLineTaxComponent does not itself carry
        # reporting_type — both components on one line share the same
        # underlying company-side TaxCode, only their direction/role
        # differ, per services.tax_engine.ee.EETaxEngine).
        role_stmt = (
            select(
                JournalLineTaxComponent.direction,
                TaxCode.reporting_type,
                JournalLineTaxComponent.base_amount,
                JournalLineTaxComponent.tax_amount,
                JournalLineTaxComponent.rate_applied,
                JournalEntry.reversal_of_id,
            )
            .join(JournalLine, JournalLineTaxComponent.journal_line_id == JournalLine.id)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .outerjoin(TaxCode, JournalLine.tax_code_id == TaxCode.id)
            .where(and_(*conditions))
        )

        role_result = await session.execute(role_stmt)
        for direction, reporting_type, base_amount, tax_amount, rate_applied, reversal_of_id in role_result.all():
            rt = reporting_type or ""
            base_amount = base_amount or Decimal("0")
            tax_amount = tax_amount or Decimal("0")
            # Finding 3 — net a reverse-charge reversal. A reversal entry
            # mirrors the original's components (same positive base/tax) onto
            # its swapped lines; here we sign the whole reversal entry's
            # contribution negative so it cancels the original's, keeping the
            # role-keyed boxes (1_RC/5_RC/…) in step with the account-type
            # boxes (6/6.1) that already net via the base's debit/credit sign.
            # Signing by reversal-status (not the line's debit/credit) is
            # direction-agnostic: it is correct whether the mirrored
            # component is output- or input-role. Normal entries have
            # reversal_of_id IS NULL → sign +1 → byte-identical to before.
            orient = Decimal(-1) if reversal_of_id is not None else Decimal(1)

            for box in role_net_index.get((direction, rt), ()):
                if not _rate_matches(box, rate_applied):
                    continue
                amounts[box.box_code] += orient * (
                    (base_amount + tax_amount) if box.inclusive else base_amount
                )
            for box in role_tax_index.get((direction, rt), ()):
                if not _rate_matches(box, rate_applied):
                    continue
                amounts[box.box_code] += orient * tax_amount

    return amounts


# ---------------------------------------------------------------------------
# ``formula`` aggregation — safe box-arithmetic AST evaluator.
# (KMD-formula support scope, Packet 1 — jurisdiction-generic; nothing here
# is EE-specific. See ~/.claude/plans/kmd-formula-support-scope.md §3.1.)
#
# Grammar (closed, minimal):
#   box ref     := <RETURN_TYPE>:<box_code> | <box_code>
#                  (bare form resolves within the CURRENT return_type; a
#                  prefixed form must name the current return_type — this
#                  packet only has one (jurisdiction, return_type) box set
#                  loaded per call, so a foreign-return_type reference has
#                  no data to resolve against and is rejected loudly)
#   literal     := decimal number, e.g. 0.24, 1, 0
#   expr        := term (('+' | '-') term)*
#   term        := unary ('*' unary)*
#   unary       := '-' unary | primary
#   primary     := literal | box_ref | 'max' '(' '0' ',' expr ')' | '(' expr ')'
#
# 'max' accepts ONLY a literal-0 first argument — the payable/refund split
# (scope's box 12/13 finding: both share one signed net, split as
# max(0,N) / max(0,-N)) — not a general function call.
#
# Box-code tokenising is longest-match against the box codes actually in
# play for this (jurisdiction, return_type), NOT a generic identifier
# regex: box codes contain '-' and '.' (e.g. "1-1", "3.1.1") which would
# otherwise collide with the subtraction operator and the decimal point.
# No box code seeded anywhere is "0" or starts with "0." (checked against
# both the AU and EE seeds), so a literal 0/0.24/... never collides with a
# code — bare-token resolution below tries a box-code match first, so this
# only matters for the never-occurring reverse collision (a formula that
# needs a bare integer literal equal to an existing bare box code); every
# real KMD formula box-references are RETURN_TYPE-prefixed, so this edge
# case does not arise in practice.
#
# FOOTGUN FOR FUTURE FORMULA AUTHORS (Packet 3+): because bare-token
# resolution tries a box-code match BEFORE a numeric literal, a bare
# integer coefficient that happens to equal/prefix an existing box code
# is silently read as a box reference, not a number — e.g. a hypothetical
# "KMD:5 * 2" would multiply by box "2"'s value if box "2" exists, not
# the literal 2. Always write coefficients in decimal form ("0.24", not
# a bare "24" or "2") — no box code starts with "0", so this sidesteps
# the ambiguity entirely and is the pattern every Packet-1 KMD formula
# already follows.
# ---------------------------------------------------------------------------


class FormulaSyntaxError(ValueError):
    """A box's ``formula`` expression could not be parsed."""


# AST node types — plain frozen dataclasses, no eval()/exec() anywhere.
@dataclass(frozen=True, slots=True)
class _FNum:
    value: Decimal


@dataclass(frozen=True, slots=True)
class _FBoxRef:
    box_code: str


@dataclass(frozen=True, slots=True)
class _FNeg:
    operand: "_FNode"


@dataclass(frozen=True, slots=True)
class _FMaxZero:
    operand: "_FNode"


@dataclass(frozen=True, slots=True)
class _FBinOp:
    op: str  # '+' | '-' | '*'
    left: "_FNode"
    right: "_FNode"


_FNode = _FNum | _FBoxRef | _FNeg | _FMaxZero | _FBinOp

_PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_NUMBER_RE = re.compile(r"\d+(\.\d+)?")


class _FormulaParser:
    """Recursive-descent parser for one box's ``formula`` expression.

    ``known_codes`` is every box_code in the current (jurisdiction,
    return_type) set (ledger AND formula boxes) — box-code tokens are
    resolved by longest-match against this set, tried longest-first so
    e.g. "3.1.1" is preferred over "3.1" over "3" when all three are
    valid codes and the text is "3.1.1...".
    """

    def __init__(self, text: str, *, return_type: str, known_codes: frozenset[str]) -> None:
        self._s = text
        self._i = 0
        self._n = len(text)
        self._return_type = return_type
        # Longest-first so prefix-of-remaining-text matching picks the
        # longest valid code, not an accidental short prefix of it.
        self._codes_longest_first = sorted(known_codes, key=len, reverse=True)

    def parse(self) -> _FNode:
        node = self._expr()
        self._skip_ws()
        if self._i != self._n:
            raise FormulaSyntaxError(
                f"unexpected trailing input {self._s[self._i:]!r} in formula {self._s!r}"
            )
        return node

    def _skip_ws(self) -> None:
        while self._i < self._n and self._s[self._i].isspace():
            self._i += 1

    def _peek_char(self) -> str:
        self._skip_ws()
        return self._s[self._i] if self._i < self._n else ""

    def _expr(self) -> _FNode:
        node = self._term()
        while True:
            c = self._peek_char()
            if c in ("+", "-"):
                self._i += 1
                node = _FBinOp(c, node, self._term())
            else:
                return node

    def _term(self) -> _FNode:
        node = self._unary()
        while True:
            c = self._peek_char()
            if c == "*":
                self._i += 1
                node = _FBinOp("*", node, self._unary())
            else:
                return node

    def _unary(self) -> _FNode:
        if self._peek_char() == "-":
            self._i += 1
            return _FNeg(self._unary())
        return self._primary()

    def _primary(self) -> _FNode:
        self._skip_ws()
        if self._i >= self._n:
            raise FormulaSyntaxError(f"unexpected end of formula {self._s!r}")
        c = self._s[self._i]

        if c == "(":
            self._i += 1
            node = self._expr()
            if self._peek_char() != ")":
                raise FormulaSyntaxError(f"expected ')' in formula {self._s!r}")
            self._i += 1
            return node

        if self._s[self._i : self._i + 3].lower() == "max" and not (
            self._i + 3 < self._n and self._s[self._i + 3].isalnum()
        ):
            return self._max_call()

        # RETURN_TYPE:box_code prefix — only recognised as a prefix when an
        # identifier is immediately followed by ':'.
        m = _PREFIX_RE.match(self._s, self._i)
        if m and m.end() < self._n and self._s[m.end()] == ":":
            prefix = m.group(0)
            if prefix.upper() != self._return_type.upper():
                raise FormulaSyntaxError(
                    f"formula {self._s!r} references return_type {prefix!r}, "
                    f"but this box belongs to {self._return_type!r} — a "
                    f"formula may only reference boxes of its own return_type"
                )
            self._i = m.end() + 1
            return _FBoxRef(self._consume_box_code())

        # Bare box code (tried before a numeric literal — see module note
        # above on why this ordering is safe for every real KMD formula).
        code = self._try_consume_box_code()
        if code is not None:
            return _FBoxRef(code)

        m2 = _NUMBER_RE.match(self._s, self._i)
        if m2:
            self._i = m2.end()
            return _FNum(Decimal(m2.group(0)))

        raise FormulaSyntaxError(
            f"unexpected token at position {self._i} in formula {self._s!r}"
        )

    def _max_call(self) -> _FNode:
        self._i += 3  # consume "max"
        if self._peek_char() != "(":
            raise FormulaSyntaxError(f"expected '(' after 'max' in formula {self._s!r}")
        self._i += 1
        self._skip_ws()
        # Only a literal 0 first argument is accepted — max(0,·) is the
        # payable/refund-split primitive, not a general function.
        if self._s[self._i : self._i + 1] != "0" or (
            self._i + 1 < self._n and self._s[self._i + 1].isdigit()
        ):
            raise FormulaSyntaxError(
                f"max(...) requires a literal 0 first argument in formula "
                f"{self._s!r} — max is only the max(0, <expr>) "
                f"payable/refund-split primitive, not a general function"
            )
        self._i += 1
        if self._peek_char() != ",":
            raise FormulaSyntaxError(f"expected ',' after 'max(0' in formula {self._s!r}")
        self._i += 1
        operand = self._expr()
        if self._peek_char() != ")":
            raise FormulaSyntaxError(f"expected ')' to close max(...) in formula {self._s!r}")
        self._i += 1
        return _FMaxZero(operand)

    def _try_consume_box_code(self) -> str | None:
        remaining = self._s[self._i :]
        for code in self._codes_longest_first:
            if remaining.startswith(code):
                self._i += len(code)
                return code
        return None

    def _consume_box_code(self) -> str:
        code = self._try_consume_box_code()
        if code is None:
            raise FormulaSyntaxError(
                f"unknown box reference at position {self._i} in formula {self._s!r}"
            )
        return code


def _formula_refs(node: _FNode) -> set[str]:
    """Every box_code referenced anywhere in an AST."""
    if isinstance(node, _FBoxRef):
        return {node.box_code}
    if isinstance(node, _FNum):
        return set()
    if isinstance(node, (_FNeg, _FMaxZero)):
        return _formula_refs(node.operand)
    if isinstance(node, _FBinOp):
        return _formula_refs(node.left) | _formula_refs(node.right)
    raise TypeError(  # pragma: no cover
        f"tax_return_generator: unhandled formula AST node {node!r}"
    )


def _eval_formula(node: _FNode, values: dict[str, Decimal]) -> Decimal:
    """Evaluate a parsed formula AST against already-computed box values.
    ``values`` must contain every box_code ``node`` references — the
    caller (``_evaluate_formula_boxes``) guarantees this via the
    topological evaluation order."""
    if isinstance(node, _FNum):
        return node.value
    if isinstance(node, _FBoxRef):
        return values[node.box_code]
    if isinstance(node, _FNeg):
        return -_eval_formula(node.operand, values)
    if isinstance(node, _FMaxZero):
        return max(Decimal("0"), _eval_formula(node.operand, values))
    if isinstance(node, _FBinOp):
        left = _eval_formula(node.left, values)
        right = _eval_formula(node.right, values)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        return left * right  # '*' — the only remaining operator
    raise TypeError(  # pragma: no cover
        f"tax_return_generator: unhandled formula AST node {node!r}"
    )


def _topological_order(deps: dict[str, frozenset[str]]) -> list[str]:
    """Kahn's algorithm over the formula-box dependency graph (``deps``
    maps a formula box_code to the *other formula box_codes* it
    references — ledger-box references don't need ordering, they're
    already computed). Raises ``ValueError`` naming a cycle path when the
    graph isn't a DAG, rather than ever silently defaulting to 0.

    Iteration order is deterministic (sorted) so evaluation order — and
    therefore any error message — doesn't depend on dict insertion order.
    """
    in_degree = {code: 0 for code in deps}
    dependents: dict[str, set[str]] = defaultdict(set)
    for code, needs in deps.items():
        for needed in needs:
            dependents[needed].add(code)
            in_degree[code] += 1

    queue: deque[str] = deque(sorted(code for code, deg in in_degree.items() if deg == 0))
    order: list[str] = []
    while queue:
        code = queue.popleft()
        order.append(code)
        for dependent in sorted(dependents.get(code, ())):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(order) != len(deps):
        remaining = set(deps) - set(order)
        cycle = _find_cycle_path(deps, remaining)
        raise ValueError(
            "tax_return_generator: formula cycle: " + " → ".join(cycle)
        )
    return order


def _find_cycle_path(deps: dict[str, frozenset[str]], remaining: set[str]) -> list[str]:
    """Best-effort DFS to name one concrete cycle (e.g. ``["4", "12", "4"]``)
    among ``remaining`` (the boxes Kahn's algorithm couldn't order) for a
    readable error message. Falls back to the sorted remaining set if,
    for any reason, no explicit cycle can be walked (defensive only —
    ``remaining`` is non-empty exactly when a cycle exists)."""
    for start in sorted(remaining):
        on_stack: set[str] = {start}
        path: list[str] = [start]

        def _dfs(node: str) -> list[str] | None:
            for needed in sorted(deps.get(node, ())):
                if needed not in remaining:
                    continue
                if needed in on_stack:
                    return path[path.index(needed) :] + [needed]
                path.append(needed)
                on_stack.add(needed)
                result = _dfs(needed)
                if result is not None:
                    return result
                path.pop()
                on_stack.discard(needed)
            return None

        found = _dfs(start)
        if found is not None:
            return found
    return sorted(remaining)  # pragma: no cover — defensive fallback


_TWO_PLACES = Decimal("0.01")

# Rounding DIRECTION is UNVERIFIED (scope §3.2): the source confirms cent
# PRECISION ("sendi täpsusega") but not the half-cent tie-break rule.
# ROUND_HALF_UP is the scope's stated pragmatic default and matches every
# other money-rounding call site in this codebase (receipts.py,
# allocations.py, bad_debt.py, deferred_revenue.py, proration.py, ...).
# Confirm against the e-MTA XSD/validation before Packet 4; if that
# surfaces ROUND_HALF_EVEN instead, this is the ONE constant to flip.
_FORMULA_ROUNDING = ROUND_HALF_UP


def _evaluate_formula_boxes(
    parsed_boxes: list[_ParsedBox],
    ledger_amounts: dict[str, Decimal],
    *,
    return_type: str,
    manual_values: dict[str, Decimal] | None = None,
) -> dict[str, Decimal]:
    """Second pass over a return's boxes: evaluate every ``formula`` box
    on top of the ledger-aggregated amounts (first pass —
    ``_aggregate_ledger_by_box``), then round every box (ledger AND
    formula) to 2dp (scope §3.2 — "Round per box after
    aggregation/evaluation, not per ledger line").

    Formula boxes may reference other formula boxes (KMD's box 12/13
    depend on box 4, itself potentially a formula — see scope §3.3), so
    this builds a dependency graph over JUST the formula boxes,
    topologically sorts it (cycle detection — ``ValueError``, never a
    silent 0), and evaluates in that order, writing each result back so
    later formulas see earlier ones.

    A jurisdiction/return_type with no formula boxes (AU/NZ/UK today) is
    a pure pass-through except for the rounding step — the regression
    proof that this pass is additive and doesn't perturb the AU
    ledger-only path.

    Manual boxes (finding 7): a ``manual`` box has no ledger recipe, so
    ``_aggregate_ledger_by_box`` initialises it to ``0`` and it stays 0 —
    an ABSENT manual value is thus an explicit ``0`` in any formula that
    references it (KMD box 12/13 read the manual boxes 4-1/10/11 this
    way). ``manual_values`` lets a caller inject filer-entered figures for
    those boxes BEFORE the formula pass, so a supplied box 4-1 flows into
    box 12/13. Only ``manual``-kind boxes may be overridden — a value for
    a ledger/formula box is rejected (its amount is engine-derived, not
    filer-entered) so a typo can't silently clobber a computed box.
    """
    amounts: dict[str, Decimal] = {
        code: value.quantize(_TWO_PLACES, rounding=_FORMULA_ROUNDING)
        for code, value in ledger_amounts.items()
    }

    if manual_values:
        manual_codes = {
            b.box_code for b in parsed_boxes if b.kind == _KIND_MANUAL
        }
        for code, value in manual_values.items():
            if code not in manual_codes:
                raise ValueError(
                    f"tax_return_generator: manual_values may only override "
                    f"'manual'-kind boxes; box {code!r} is not manual (or "
                    f"does not exist in this return)."
                )
            amounts[code] = Decimal(value).quantize(
                _TWO_PLACES, rounding=_FORMULA_ROUNDING
            )

    formula_boxes = {b.box_code: b for b in parsed_boxes if b.kind == _KIND_FORMULA}
    if not formula_boxes:
        return amounts

    known_codes = frozenset(b.box_code for b in parsed_boxes)
    asts: dict[str, _FNode] = {}
    deps: dict[str, frozenset[str]] = {}
    for code, box in formula_boxes.items():
        assert box.formula is not None  # guaranteed by _parse_box_definition
        try:
            ast = _FormulaParser(
                box.formula, return_type=return_type, known_codes=known_codes
            ).parse()
        except FormulaSyntaxError as exc:
            raise ValueError(
                f"tax_return_generator: box {code!r} has an invalid formula "
                f"{box.formula!r}: {exc}"
            ) from exc
        # _FormulaParser resolves every box_ref token by longest-match
        # against known_codes (see its docstring), so refs here is always
        # <= known_codes by construction — an out-of-set reference (e.g.
        # box 4 formula pointing at a nonexistent box 99) already raised
        # FormulaSyntaxError above, at the exact token position, instead
        # of surfacing here as a vague "unknown box(es)" set.
        asts[code] = ast
        deps[code] = frozenset(_formula_refs(ast) & formula_boxes.keys())

    for code in _topological_order(deps):
        value = _eval_formula(asts[code], amounts)
        amounts[code] = value.quantize(_TWO_PLACES, rounding=_FORMULA_ROUNDING)

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
    manual_values: dict[str, Decimal] | None = None,
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
    ledger_amounts = await _aggregate_ledger_by_box(
        session,
        parsed,
        company_id=company_id,
        tenant_id=tenant_id,
        from_date=from_date,
        to_date=to_date,
        statuses=statuses,
        exclude_archived=exclude_archived,
    )
    amounts = _evaluate_formula_boxes(
        parsed, ledger_amounts, return_type=return_type, manual_values=manual_values
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


async def persist_return(
    session: AsyncSession,
    result: TaxReturnResult,
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    period_id: uuid.UUID,
    status: TaxReturnStatus = TaxReturnStatus.READY,
    generated_by_user_id: uuid.UUID | None = None,
) -> TaxReturn:
    """Persist a computed ``TaxReturnResult`` to ``tax_returns`` (company DB).

    Jurisdiction/return-type generic — nothing here is KMD- or BAS-specific
    (KMD-formula support scope §4/Packet 4: "Persist generated returns the
    way the scope specifies ... AGPL-side only"). ``figures`` is a JSONB
    dict keyed by ``box_code``, each value carrying ``amount`` (string, to
    preserve ``Decimal`` precision through JSON round-tripping — JSONB has
    no decimal type), ``label``, and ``display_order`` — the same nested
    ``{"amount": ...}`` shape ``sbr.bas.BasFigures.from_figures_json`` and
    ``lodgement.kmd.KmdFigures.from_figures_json`` already tolerate on read.

    Defaults to ``TaxReturnStatus.READY`` (not ``DRAFT``) — a
    ``TaxReturnResult`` is a fully-computed return, not a work-in-progress
    shell; ``api/v1/tax_returns.py``'s ``POST /tax_returns`` endpoint is the
    separate DRAFT-creation path for callers building figures by hand.

    Does not commit — caller controls the transaction boundary (mirrors
    every other write helper in this module's neighbourhood, e.g.
    ``services.journal.post``).

    Critic-round-3 fix: excludes engine-internal aggregation-leg boxes
    (``display_order >= 100`` — the seed convention
    ``services.lodgement.kmd.mapping``'s module docstring documents for
    the EE KMD seed's ``1_DOMESTIC``/``1_RC``/``5_DOMESTIC``/``5_RC``
    helper boxes that feed the box-1/box-5 BOX-FORMULA) from the
    persisted ``figures``. These are engine-internal aggregation legs,
    not fields on any filable form, and the KMD-specific serializer
    already excludes them via ``KMD_BOX_ORDER``; but the generic
    ``GET /tax_returns`` read API (``api/v1/tax_returns.py::
    _serialise_return``) echoes ``figures`` raw, so leaving them in would
    surface 4 non-form codes — one labelled "(internal) ..." — to any
    caller that renders "the return" by iterating ``figures`` directly.
    Jurisdiction/return-type generic: any return type may use the same
    ``display_order >= 100`` convention for its own internal legs.
    """
    figures = {
        box_code: {
            "amount": str(box.amount),
            "label": box.box_label,
            "display_order": box.display_order,
        }
        for box_code, box in result.boxes.items()
        if box.display_order < 100
    }
    row = TaxReturn(
        company_id=company_id,
        tenant_id=tenant_id,
        jurisdiction=result.jurisdiction,
        period_id=period_id,
        return_type=result.return_type,
        figures=figures,
        status=status,
        generated_by_user_id=generated_by_user_id,
    )
    session.add(row)
    await session.flush()
    return row


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
    manual_values: dict[str, Decimal] | None = None,
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
    ledger_amounts = await _aggregate_ledger_by_box(
        session,
        parsed,
        company_id=company_id,
        tenant_id=tenant_id,
        from_date=from_date,
        to_date=to_date,
        statuses=statuses,
        exclude_archived=exclude_archived,
    )
    return _evaluate_formula_boxes(
        parsed, ledger_amounts, return_type=return_type, manual_values=manual_values
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
