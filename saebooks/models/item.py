"""Item (tracked stock) model for inventory v1.

An Item is a per-company SKU with a costing method, an on-hand
quantity, a weighted-average unit cost (base currency), a default
sale price, and the three GL accounts that stock movements post
against:

* ``inventory_account_id`` — asset account that Dr on receipt / Cr
  on issue (typically ``1-1330 Trading Stock on Hand``).
* ``cogs_account_id`` — expense account that Dr at WAC on sale
  (typically ``5-0000 Cost of sales``).
* ``income_account_id`` — income account that the invoice line
  credits at sale price (typically ``4-0000 Income``).

Lines that carry an ``item_id`` will force ``account_id`` to these
at post time so the GL can never drift from inventory. Lines without
``item_id`` are service-only and behave as before (user picks the
account directly).

Cost methods for v1: only ``WAC`` (weighted-average cost). Python
validates the value; the DB also enforces via CHECK constraint.

Soft-delete via ``archived_at`` mirrors Contact — hard-delete would
orphan historical invoice / bill lines.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base
from saebooks.models._scope import CompanyScoped


class CostMethod(enum.StrEnum):
    WAC = "WAC"
    # Future: FIFO = "FIFO"; STANDARD = "STANDARD"


class ItemType(enum.StrEnum):
    INVENTORY = "inventory"  # tracked stock with on_hand_qty + WAC
    SERVICE = "service"      # non-stocked — no stock movements


class Item(CompanyScoped, Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("company_id", "sku", name="uq_items_company_sku"),
        CheckConstraint(
            "cost_method IN ('WAC')",
            name="ck_items_cost_method_valid",
        ),
        CheckConstraint(
            "item_type IN ('inventory', 'service')",
            name="ck_items_item_type_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    item_type: Mapped[ItemType] = mapped_column(
        String(16), nullable=False, default=ItemType.INVENTORY
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    cost_method: Mapped[CostMethod] = mapped_column(
        String(16), nullable=False, default=CostMethod.WAC
    )
    on_hand_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    wac_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    default_sale_price: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    inventory_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cogs_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    income_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Optimistic-locking version — bumped on every write through the API.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"<Item {self.sku} {self.name} on_hand={self.on_hand_qty}>"
