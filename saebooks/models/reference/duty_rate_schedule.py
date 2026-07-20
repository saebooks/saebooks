"""Transfer/stamp/conveyance duty rates by jurisdiction, state and
transaction type (M1.5 · Wave 3a rename hygiene).

Renamed from ``stamp_duty_rate`` / ``StampDutyRate`` — no canonical
successor exists (it IS the canonical rate-lookup table, just AU-named),
and unlike the drop set it has live consumers: ``services/dutiable_events.py``
(``lookup_stamp_duty_rate``, kept as the function name — only the
model/table changed) and the docstrings in
``models/dutiable_transaction_event.py``, updated in this same migration
so nothing breaks.

Effective-dating (M1.5 · 5-DUTIES, reference migration 0014): rows may
carry ``effective_from`` / ``effective_to`` so a rate change lands as a
new dated row instead of an in-place edit. Both NULLABLE — every
pre-existing row (and any caller not passing ``as_at`` to
``lookup_stamp_duty_rate``) keeps its undated open-ended semantics, and
the natural-key uniqueness constraint only bites dated series (Postgres
treats NULL ``effective_from`` values as distinct).
"""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class DutyRateSchedule(ReferenceBase):
    __tablename__ = "duty_rate_schedules"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "state", "transaction_type", "lower_bound",
            "effective_from",
            name="uq_duty_rate_schedules_natkey",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(8), nullable=False)
    # M1.5 · 5-SUBJURIS (reference migration 0016): FK promotion of the
    # ad-hoc ``state`` string into the T3 jurisdiction tree. NULLABLE and
    # additive — ``state`` stays in the natural key and authoritative for
    # ``lookup_stamp_duty_rate`` during the transition; AU rows are
    # backfilled ('QLD' → 'AU-QLD').
    sub_jurisdiction_code: Mapped[str | None] = mapped_column(
        String(6),
        ForeignKey("jurisdictions.code"),
        comment="Sub-national jurisdiction node (T3 tree), e.g. 'AU-QLD'.",
    )
    transaction_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "property_transfer | motor_vehicle | insurance | mortgage | "
            "securities | lease | landholder_acquisition"
        ),
    )
    lower_bound: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    upper_bound: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    effective_from: Mapped[date | None] = mapped_column(
        Date, comment="NULL = undated legacy row (no effective-dating)."
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, comment="NULL = still in force."
    )
