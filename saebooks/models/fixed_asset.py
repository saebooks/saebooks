"""Fixed asset register.

A ``FixedAsset`` row combines the classical-accounting fields (cost,
depreciation model, paired cost/accum-dep/expense accounts) with
physical-tracking fields (serial number, location, custody, warranty).

Lifecycle:

1. **Created** — status ``active``, ``last_depreciation_posted_through``
   is NULL until the first depreciation run.
2. **Depreciating** — the ``post_depreciation`` service posts a
   Dr Depreciation Expense / Cr Accumulated Depreciation journal entry
   and advances ``last_depreciation_posted_through``. Idempotent:
   re-running with the same ``through_date`` is a no-op.
3. **Disposed** — ``dispose_asset`` posts the closeout journal
   (clears cost + accum-dep, credits proceeds, books gain/loss),
   sets status ``disposed``, stamps ``disposal_*`` fields.
4. **Archived** — soft-delete via ``archived_at``; the row and its
   journal trail stay intact.

See ``saebooks/services/assets.py`` for the business-logic layer.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saebooks.db import Base
from saebooks.db_types import Money
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

if TYPE_CHECKING:
    from saebooks.models.depreciation_model import DepreciationModel


class FixedAsset(CompanyScoped, Base):
    """One row per capitalised asset.

    Status values: ``active`` (in use, may depreciate), ``disposed``
    (closeout journal posted), ``archived`` (soft-deleted from the
    list view but journal trail intact).

    Three ``*_account_id`` columns tie this asset to GL accounts:

    * ``cost_account_id`` — usually a ``1-31xx`` row (e.g.
      ``1-3310 Office Equipment``) where the asset's cost sits as a
      debit balance.
    * ``accum_dep_account_id`` — the paired ``1-31x0 Accum Dep`` row
      (contra-asset; depreciation credits accumulate here).
    * ``dep_expense_account_id`` — the P&L account that takes the
      periodic depreciation charge. Defaults to ``6-1500 Depreciation
      Expense`` on creation.

    Disposal requires a fourth account (where the sale proceeds land
    — usually a bank account) but that's passed per-disposal rather
    than stored on the asset.
    """

    __tablename__ = "fixed_assets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=lambda: _DEFAULT_TENANT_ID,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Short identifier — e.g. FA-0001; auto-generated on create",
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # ---- GL coordinates ---------------------------------------------- #
    cost_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    accum_dep_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dep_expense_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    depreciation_model_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("depreciation_models.id", ondelete="RESTRICT"),
        nullable=False,
        comment=(
            "Book depreciation model — management/GL cadence. Drives the "
            "journals posted by post_depreciation()."
        ),
    )
    tax_model_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("depreciation_models.id", ondelete="RESTRICT"),
        nullable=True,
        comment=(
            "Optional tax depreciation model (e.g. asset_dv_30). NULL = "
            "tax schedule matches book. Reporting-only overlay, never "
            "touches GL."
        ),
    )

    # ---- Money / dates ---------------------------------------------- #
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    in_service_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="Depreciation clock starts from here; defaults to purchase_date",
    )
    cost: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    residual_value: Mapped[Decimal] = mapped_column(
        Money(),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
        comment="Salvage value; depreciation base = cost - residual",
    )
    last_depreciation_posted_through: Mapped[date | None] = mapped_column(
        Date,
        comment="High-water mark for posted depreciation; NULL = never depreciated",
    )

    # Acquisition-cost component breakdown (M1.5 P1 tail) — optional
    # itemisation of ``cost`` above, which remains the sole authoritative
    # total the depreciation/disposal math reads. NULL on every column =
    # not itemised (every existing asset). Not enforced to sum to
    # ``cost`` — record-keeping only.
    purchase_price_component: Mapped[Decimal | None] = mapped_column(
        Money(), nullable=True, comment="Base purchase price, excl. duty/installation"
    )
    duty_component: Mapped[Decimal | None] = mapped_column(
        Money(), nullable=True, comment="Stamp/import duty paid on acquisition"
    )
    installation_component: Mapped[Decimal | None] = mapped_column(
        Money(), nullable=True, comment="Installation/commissioning cost to bring the asset to use"
    )

    # ---- Lifecycle --------------------------------------------------- #
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
        comment="active | disposed | archived",
    )
    disposal_date: Mapped[date | None] = mapped_column(Date)
    disposal_proceeds: Mapped[Decimal | None] = mapped_column(Money())
    disposal_journal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="SET NULL"),
    )
    parent_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fixed_assets.id", ondelete="SET NULL"),
        comment=(
            "Self-ref: points at the original asset when this row is the "
            "disposed-fraction child from a partial disposal."
        ),
    )

    # ---- Physical tracking ------------------------------------------ #
    serial_number: Mapped[str | None] = mapped_column(String)
    manufacturer: Mapped[str | None] = mapped_column(String)
    model_number: Mapped[str | None] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String)
    custody_person: Mapped[str | None] = mapped_column(
        String,
        comment="Free-text for v1; upgrade to FK when employees model lands",
    )
    warranty_end: Mapped[date | None] = mapped_column(Date)
    purchase_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        comment="Supplier that sold the asset — optional",
    )

    # ---- Unstructured + audit --------------------------------------- #
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    depreciation_model: Mapped[DepreciationModel] = relationship(
        back_populates="assets",
        foreign_keys=[depreciation_model_id],
    )
