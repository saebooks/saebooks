"""Depreciation-model catalogue.

A depreciation model is the schedule used to amortise an asset's cost
over its useful life — e.g. "5-year linear" expenses ``cost / 60`` per
month for 60 months. Models are shared across the whole tenant (no
``company_id``) because they're jurisdiction-level (AU tax rules,
in this codebase) rather than company policy.

The catalogue is seeded from
``saebooks/seed/au/account.depreciation.model-au.csv`` and currently
ships six rows: ``asset_no_depreciation`` plus linear models for
3/4/5/10/20 years. Future methods (diminishing value, low-value pool)
get added here without schema change — ``method`` is a free string and
``method_progress_factor`` is reserved for DV rates.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base

if TYPE_CHECKING:
    from saebooks.models.fixed_asset import FixedAsset


class DepreciationModel(Base):
    """One row per seeded depreciation schedule.

    ``id`` is a human-readable slug (e.g. ``asset_5_year_linear``)
    rather than a UUID so the AU CoA seed CSV can reference models by
    slug without needing a lookup pass. Slugs are stable across
    installs because the seed is version-controlled.
    """

    __tablename__ = "depreciation_models"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    method: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="'no_depreciation' or 'linear' for v1; DV/etc land here later",
    )
    method_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Useful-life value — years for linear, 0 for no_depreciation",
    )
    method_period: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Periods per method_number unit — 12 for monthly-over-N-years",
    )
    method_progress_factor: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4),
        comment="Reserved for diminishing-value rate (e.g. 2.0 for 200% DV)",
    )
    rate_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 4),
        comment=(
            "Annual DV percentage (e.g. 30.0000 for 30%). NULL for linear / "
            "no-depreciation models."
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    assets: Mapped[list[FixedAsset]] = relationship(
        back_populates="depreciation_model"
    )
