"""Foreign / non-resident purchaser surcharge duty rates (M1.5 · 5-DUTIES).

A per-jurisdiction catalog of the ADDITIONAL duty a purchaser-class
attracts on top of the base transfer duty computed from
``duty_rate_schedules`` — e.g. Queensland's Additional Foreign Acquirer
Duty (AFAD), NSW Surcharge Purchaser Duty, Victoria's Foreign Purchaser
Additional Duty. Sibling of ``DutyRateSchedule``: same
jurisdiction/sub-jurisdiction/transaction_type vocabulary so a caller
resolving a base bracket can resolve the surcharge with the same keys.

Rows are effective-dated (natural key includes ``effective_from``,
mirroring ``oss_member_state_rates``' dated-rate-series convention);
``effective_to`` NULL = still in force. ``sub_jurisdiction`` uses the
same value vocabulary as ``duty_rate_schedules.state`` ('QLD', 'NSW',
...); country-wide surcharges (e.g. a national additional buyer's stamp
duty) use 'ALL'.

The company-side consumer is ``DutiableTransactionEvent.surcharge_duty``
/ ``applied_surcharge_rate_id`` (opaque, non-FK — the reference DB is a
separate database), resolved via
``jurisdictions.au.dutiable_events.lookup_duty_surcharge_rate``.
"""
import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class SurchargePurchaserClass(enum.StrEnum):
    """The purchaser class that triggers the surcharge."""

    FOREIGN_PERSON = "foreign_person"    # foreign natural person/corp/trust
    ABSENTEE_OWNER = "absentee_owner"    # non-ordinarily-resident owner
    NON_RESIDENT = "non_resident"        # tax-residency-based test


SURCHARGE_PURCHASER_CLASSES = tuple(c.value for c in SurchargePurchaserClass)


class RefDutySurchargeRate(ReferenceBase):
    """One purchaser-class surcharge rate in one (sub-)jurisdiction for a
    dated period. NOT per-company — a ``DutiableTransactionEvent``
    references a row from here by id (opaque, no cross-DB FK)."""

    __tablename__ = "duty_surcharge_rates"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "sub_jurisdiction", "transaction_type",
            "purchaser_class", "effective_from",
            name="uq_duty_surcharge_rates_natkey",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    sub_jurisdiction: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment=(
            "Same vocabulary as duty_rate_schedules.state ('QLD', 'NSW', "
            "...); 'ALL' for a country-wide surcharge."
        ),
    )
    transaction_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Mirrors duty_rate_schedules.transaction_type, e.g. property_transfer.",
    )
    purchaser_class: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of SURCHARGE_PURCHASER_CLASSES.",
    )
    surcharge_rate: Mapped[Decimal] = mapped_column(
        Numeric(7, 4),
        nullable=False,
        comment="Percentage of dutiable value (8.0000 = 8%, not 0.08).",
    )
    land_use_scope: Mapped[str | None] = mapped_column(
        String(32),
        comment="residential | primary_production | all — NULL if unrestricted.",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(
        Date, comment="NULL = still in force."
    )
    description: Mapped[str | None] = mapped_column(String(512))
