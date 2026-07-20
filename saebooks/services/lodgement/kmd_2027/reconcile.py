"""Local koondvaade reconciliation for the 2027 data-based KMD.

In the 2027 regime EMTA derives the KMD boxes and the payable/refund bottom line
SERVER-SIDE from the transmitted transaction rows (andmepohine read §1/§4). The
taxpayer's trust surface is a local recompute: run the SAME posted ledger through
the existing box engine (``tax_return_generator.generate_return``), and check
that the exported transaction rows sum, by category, to the box figures the user
would otherwise have filed — so they can sanity-check EMTA's computed
koondvaade before confirming.

This module is the box engine "earning its keep" in the 2027 world
(build-plan §4.2). The pure core ``reconcile(rows, box_amounts)`` is
DB-free and unit-testable; ``reconcile_period(...)`` is the thin DB wrapper that
wires the generator + box engine together (postgres_only).

Honesty rule (advisor): a row whose KMDTYYP leaf falls into NO reconciliation
group is SURFACED in ``unclassified_rows`` — never silently dropped — and
``reconciled`` is False whenever that list is non-empty. Otherwise an exotic /
unmapped transaction could vanish while the totals still tie.

READY FOR the 2027 data-based KMD; NOT "compliant with" (VTK-stage law).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from saebooks.services.lodgement.kmd_2027 import kmdtyyp
from saebooks.services.lodgement.kmd_2027.serializer import Kmd2027Row

_ZERO = Decimal("0")

# Each koondvaade reconciliation line sums the exported rows of one
# ``reconcile_group`` against a set of KMD box codes. The box codes reference
# the box engine's DOMESTIC sub-boxes (``1_DOMESTIC`` …) rather than the top
# formula boxes (``1`` …) deliberately: the top box 1 also absorbs the
# reverse-charge OUTPUT component (``1_RC`` feeder), whereas a reverse-charge
# acquisition is exported as an S_* row (``rc_acquisition`` group), not an M_101
# row — so domestic supply must reconcile to the DOMESTIC leaves only, or the
# RC output would be double-counted. This is why ``reconcile`` needs the RAW box
# vector (every parsed box, incl. the display_order>=100 internal aggregation
# legs), NOT the trimmed filable ``KmdFigures``.
GROUP_TO_BOXES: dict[str, tuple[str, ...]] = {
    # Domestic standard/reduced supply taxable value (all rate vintages).
    "domestic_taxable_supply": (
        "1_DOMESTIC", "1-1", "1-2", "2_DOMESTIC", "2-1", "2-2_DOMESTIC",
    ),
    # §41^1 seller-side reverse charge + goods installed in another MS (lahter 9).
    "domestic_rc_supply": ("9",),
    # Zero-rated supply (intra-Community, export, tax-free) — lahter 3.
    "zero_rated_supply": ("3",),
    # Exempt supply — lahter 8.
    "exempt_supply": ("8",),
    # Reverse-charge acquisition self-assessed base — lahter 6 (IC) + 7 (other).
    "rc_acquisition": ("6", "7"),
    # Total deductible input VAT — lahter 5.
    "input_vat": ("5",),
}


@dataclass(frozen=True)
class ReconcileLine:
    """One category reconciliation: exported-row total vs box-engine total."""

    group: str
    box_codes: tuple[str, ...]
    rows_total: Decimal
    boxes_total: Decimal

    @property
    def delta(self) -> Decimal:
        return self.rows_total - self.boxes_total


@dataclass(frozen=True)
class UnclassifiedRow:
    """An exported row whose KMDTYYP leaf has no reconciliation group — flagged,
    not dropped (a leaf with ``reconcile_group: null``, or an unknown code)."""

    line_number: int
    kmdtyyp_code: str
    amount: Decimal
    reason: str


@dataclass(frozen=True)
class ReconcileReport:
    lines: list[ReconcileLine] = field(default_factory=list)
    unclassified_rows: list[UnclassifiedRow] = field(default_factory=list)
    tolerance: Decimal = _ZERO

    @property
    def reconciled(self) -> bool:
        """True only if every category ties within tolerance AND no exported row
        was left unclassified."""
        if self.unclassified_rows:
            return False
        return all(abs(line.delta) <= self.tolerance for line in self.lines)

    def line_for(self, group: str) -> ReconcileLine | None:
        return next((ln for ln in self.lines if ln.group == group), None)


def reconcile(
    rows: list[Kmd2027Row],
    box_amounts: dict[str, Decimal],
    *,
    tolerance: Decimal = _ZERO,
) -> ReconcileReport:
    """Pure koondvaade reconciliation.

    ``rows`` — the exported EE0203001 transaction rows.
    ``box_amounts`` — the RAW box vector from ``tax_return_generator`` keyed by
      box_code (must include the DOMESTIC sub-boxes; missing codes count as 0).
    ``tolerance`` — per-line absolute allowance (default exact); the report
      always carries the signed delta so drift is visible regardless.
    """
    group_totals: dict[str, Decimal] = {g: _ZERO for g in GROUP_TO_BOXES}
    unclassified: list[UnclassifiedRow] = []

    for row in rows:
        leaf = kmdtyyp.leaf_meta(row.kmdtyyp_code)
        if leaf is None:
            unclassified.append(UnclassifiedRow(
                line_number=row.line_number, kmdtyyp_code=row.kmdtyyp_code,
                amount=row.amount,
                reason=f"unknown KMDTYYP leaf {row.kmdtyyp_code!r} (not in the taxonomy seed)",
            ))
            continue
        group = leaf.reconcile_group
        if group is None:
            unclassified.append(UnclassifiedRow(
                line_number=row.line_number, kmdtyyp_code=row.kmdtyyp_code,
                amount=row.amount,
                reason=(
                    f"leaf {row.kmdtyyp_code} has no koondvaade reconciliation group "
                    "(informative / accounting-entry leaf) — verify manually"
                ),
            ))
            continue
        if group not in group_totals:
            unclassified.append(UnclassifiedRow(
                line_number=row.line_number, kmdtyyp_code=row.kmdtyyp_code,
                amount=row.amount,
                reason=f"leaf {row.kmdtyyp_code} maps to unknown reconcile group {group!r}",
            ))
            continue
        group_totals[group] += row.amount

    lines: list[ReconcileLine] = []
    for group, box_codes in GROUP_TO_BOXES.items():
        boxes_total = sum((box_amounts.get(code, _ZERO) for code in box_codes), _ZERO)
        lines.append(ReconcileLine(
            group=group, box_codes=box_codes,
            rows_total=group_totals[group], boxes_total=boxes_total,
        ))

    return ReconcileReport(lines=lines, unclassified_rows=unclassified, tolerance=tolerance)


async def reconcile_period(
    session,
    *,
    company_id: uuid.UUID,
    period_start: date,
    period_end: date,
    tolerance: Decimal = _ZERO,
) -> ReconcileReport:
    """DB wrapper: export the period's rows via the generator, recompute the KMD
    box vector via the box engine, and reconcile the two.

    Lazy-imports the DB-bound generator + box engine so this module stays
    importable (and the pure ``reconcile`` above stays testable) without a DB.
    postgres_only.
    """
    from saebooks.services.lodgement.kmd_2027.generator import generate_kmd_2027
    from saebooks.services.tax_return_generator import generate_return

    listing = await generate_kmd_2027(
        session, company_id=company_id,
        period_start=period_start, period_end=period_end,
    )
    result = await generate_return(
        session, company_id,
        jurisdiction="EE", return_type="KMD",
        from_date=period_start, to_date=period_end,
    )
    box_amounts = {code: box.amount for code, box in result.boxes.items()}
    return reconcile(listing.rows, box_amounts, tolerance=tolerance)
