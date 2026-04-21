"""Budget model — monthly amount per (company, account, year, month).

Used by the budget-vs-actual report in :mod:`saebooks.services.reports`.

Granularity decision: **monthly**. AU BAS is quarterly, ops reporting
is monthly, annual budgets are just twelve identical rows — so monthly
is the most flexible without being too fine-grained.

Unique key ``(company_id, account_id, year, month)`` — upsert is the
primary write path (see ``services/budgets.py:upsert``). Editing a
whole-year grid for one account is a twelve-row bulk upsert.

Amount stored as ``Numeric(18, 2)`` for consistency with all new money
columns. Budgets do NOT hit the GL — they're a reporting overlay only.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "account_id",
            "year",
            "month",
            name="uq_budgets_company_account_year_month",
        ),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_budgets_month_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
