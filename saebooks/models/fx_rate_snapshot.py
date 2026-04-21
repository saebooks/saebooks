"""Cached FX rate snapshot.

One row per ``(rate_date, source, from_ccy, to_ccy)``. The
``services/fx/rates.py`` lookup path reads-through this cache before
hitting the upstream feed (RBA by default, ECB/custom in future)
so unit tests + repeat lookups stay deterministic and free of
network chatter.

Deliberately NOT ``CompanyScoped`` — FX rates are global infra, not
book-of-account data. Multiple companies in the same install share
the snapshot table.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import Base


class FxRateSnapshot(Base):
    __tablename__ = "fx_rate_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "rate_date",
            "source",
            "from_ccy",
            "to_ccy",
            name="uq_fx_rate_snapshots_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rate_date: Mapped[date]
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    from_ccy: Mapped[str] = mapped_column(String(3), nullable=False)
    to_ccy: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
