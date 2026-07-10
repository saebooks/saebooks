"""Perpetual FIFO cost layer (Wave D, 2026-07-10).

One row per stock RECEIPT for a company whose ``costing_method`` is
``fifo``. A receipt creates a layer (``original_qty`` @ ``unit_cost``,
dated ``received_date``); an issue consumes layers oldest-first,
decrementing ``remaining_qty`` and posting COGS from the consumed
layers via the existing journal chokepoint (never a manual JE).

Tenant-scoped table following the non-negotiable new-table RLS checklist
(same shape as ``dutiable_transaction_events`` / migration 0182):

* ``tenant_id`` NOT NULL + FK ``tenants`` (RESTRICT) and ``company_id``
  NOT NULL + FK ``companies`` (CASCADE).
* ENABLE + FORCE ROW LEVEL SECURITY + the standard ``tenant_isolation``
  policy (verbatim ``app.current_tenant`` predicate + WITH CHECK).
* The 0131 ``assert_child_tenant_matches_company`` coherence trigger so
  ``tenant_id`` can never disagree with ``companies.tenant_id`` for the
  row's ``company_id``.
* A composite ``(item_id, company_id)`` -> ``items(id, company_id)`` FK
  so the DB itself refuses a layer that points at a sister company's
  item.
* Explicit GRANT to ``saebooks_app`` (default privileges miss tables
  created under the non-owner migration role).

``CompanyScoped`` so the application-layer tenant filter
(``services.tenant``) also scopes every ORM query, matching ``Item``.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped

_DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class InventoryCostLayer(CompanyScoped, Base):
    __tablename__ = "inventory_cost_layers"
    __table_args__ = (
        # Cross-company guard: the layer's item must belong to the layer's
        # company. Targets uq_items_id_company (added in migration 0186).
        ForeignKeyConstraint(
            ["item_id", "company_id"],
            ["items.id", "items.company_id"],
            name="fk_inventory_cost_layers_item_company",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        default=_DEFAULT_TENANT_ID,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    # item_id is FK'd via the composite ForeignKeyConstraint above.
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Units received in this layer (immutable — the original receipt qty).
    original_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False
    )
    # Units still available to be consumed (drops to 0 as issues consume
    # this layer oldest-first). A fully-consumed layer stays as a 0-remaining
    # row for audit/history rather than being deleted.
    remaining_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False
    )
    # Base-currency unit cost captured at receipt (NUMERIC(18,4) — inventory
    # unit costs are frequently sub-cent, same as items.wac_cost).
    unit_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return (
            f"<InventoryCostLayer item={self.item_id} "
            f"rem={self.remaining_qty}/{self.original_qty} @ {self.unit_cost}>"
        )
