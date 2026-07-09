"""Daily FX snapshots from RBA / ECB / OANDA etc.

Named ``RefFxRateSnapshot`` to avoid a class-name collision with the
existing company-DB model ``saebooks.models.fx_rate_snapshot.FxRateSnapshot``.
The two tables serve different purposes: the company-DB one is a record
of the rate USED on a transaction; this one is the reference set the app
*pulls* from.
"""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class RefFxRateSnapshot(ReferenceBase):
    __tablename__ = "fx_rate_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date", "base_currency", "quote_currency", "source",
            name="uq_ref_fx_date_pair_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    quote_currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="rba | ecb | oanda | manual"
    )
