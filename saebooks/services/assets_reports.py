"""Fixed-asset reporting overlay (Batch NN).

Generates the depreciation schedule — one row per active or disposed
asset with book + tax cumulative depreciation as of a given date.
Used by the BAS preparer to compute the temporary tax-vs-book
difference for deferred-tax disclosure.

Pure read-only service; no GL side-effects. Tax cumulative is
computed on-demand from ``(cost, residual, in_service_date,
tax_model_id or depreciation_model_id)`` — same math as book, just
with a potentially different model. Assets with NULL ``tax_model_id``
show book == tax and a zero difference column.
"""
from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saebooks.models.fixed_asset import FixedAsset
from saebooks.services import assets as asset_svc


@dataclass(frozen=True)
class DepreciationScheduleRow:
    asset_id: uuid.UUID
    code: str
    name: str
    status: str
    cost: Decimal
    residual: Decimal
    book_model_id: str
    tax_model_id: str | None
    book_cumulative: Decimal
    tax_cumulative: Decimal

    @property
    def book_nbv(self) -> Decimal:
        return (self.cost - self.book_cumulative).quantize(Decimal("0.01"))

    @property
    def tax_written_down_value(self) -> Decimal:
        return (self.cost - self.tax_cumulative).quantize(Decimal("0.01"))

    @property
    def temporary_difference(self) -> Decimal:
        """Book NBV minus tax WDV — positive means deferred tax liability."""
        return (self.book_nbv - self.tax_written_down_value).quantize(
            Decimal("0.01")
        )


@dataclass(frozen=True)
class DepreciationSchedule:
    as_at: date
    rows: list[DepreciationScheduleRow]

    @property
    def total_cost(self) -> Decimal:
        return sum((r.cost for r in self.rows), Decimal("0")).quantize(
            Decimal("0.01")
        )

    @property
    def total_book_cumulative(self) -> Decimal:
        return sum((r.book_cumulative for r in self.rows), Decimal("0")).quantize(
            Decimal("0.01")
        )

    @property
    def total_tax_cumulative(self) -> Decimal:
        return sum((r.tax_cumulative for r in self.rows), Decimal("0")).quantize(
            Decimal("0.01")
        )

    @property
    def total_temporary_difference(self) -> Decimal:
        return sum(
            (r.temporary_difference for r in self.rows), Decimal("0")
        ).quantize(Decimal("0.01"))


async def depreciation_schedule(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    as_at: date,
    include_disposed: bool = False,
) -> DepreciationSchedule:
    """Build the depreciation schedule for all non-archived assets.

    ``include_disposed`` — by default disposed assets are hidden, since
    their cumulative depreciation is moot after the disposal journal
    cleared the GL. Pass True for historical views.
    """
    stmt = select(FixedAsset).where(
        FixedAsset.company_id == company_id,
        FixedAsset.archived_at.is_(None),
    )
    if not include_disposed:
        stmt = stmt.where(FixedAsset.status == "active")
    stmt = stmt.order_by(FixedAsset.code)

    assets = (await session.execute(stmt)).scalars().all()

    rows: list[DepreciationScheduleRow] = []
    for a in assets:
        book_cum = await asset_svc.cumulative_depreciation_through(
            session, a, as_at
        )
        tax_cum = await asset_svc.cumulative_tax_depreciation_through(
            session, a, as_at
        )
        rows.append(
            DepreciationScheduleRow(
                asset_id=a.id,
                code=a.code,
                name=a.name,
                status=a.status,
                cost=a.cost,
                residual=a.residual_value,
                book_model_id=a.depreciation_model_id,
                tax_model_id=a.tax_model_id,
                book_cumulative=book_cum,
                tax_cumulative=tax_cum,
            )
        )
    return DepreciationSchedule(as_at=as_at, rows=rows)


def depreciation_schedule_csv(schedule: DepreciationSchedule) -> str:
    """Emit the schedule as a 10-column CSV."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "code",
            "name",
            "status",
            "cost",
            "book_model_id",
            "book_cumulative",
            "book_nbv",
            "tax_model_id",
            "tax_cumulative",
            "temporary_difference",
        ]
    )
    for r in schedule.rows:
        w.writerow(
            [
                r.code,
                r.name,
                r.status,
                f"{r.cost:.2f}",
                r.book_model_id,
                f"{r.book_cumulative:.2f}",
                f"{r.book_nbv:.2f}",
                r.tax_model_id or "",
                f"{r.tax_cumulative:.2f}",
                f"{r.temporary_difference:.2f}",
            ]
        )
    return buf.getvalue()


__all__ = [
    "DepreciationSchedule",
    "DepreciationScheduleRow",
    "depreciation_schedule",
    "depreciation_schedule_csv",
]
