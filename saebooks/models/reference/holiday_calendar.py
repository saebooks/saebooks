"""Public holidays — used for due-date arithmetic and business-day shifts."""
import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class HolidayCalendar(ReferenceBase):
    __tablename__ = "holiday_calendars"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    state: Mapped[str | None] = mapped_column(
        String(8), comment="NULL = national holiday; populated for state-specific"
    )
    # M1.5 · 5-SUBJURIS (reference migration 0016): FK promotion of the
    # ad-hoc ``state`` string into the T3 jurisdiction tree. NULLABLE and
    # additive — ``state`` stays authoritative for existing callers during
    # the transition; AU rows are backfilled ('QLD' → 'AU-QLD').
    sub_jurisdiction_code: Mapped[str | None] = mapped_column(
        String(6),
        ForeignKey("jurisdictions.code"),
        comment="Sub-national jurisdiction node (T3 tree), e.g. 'AU-QLD'. "
        "NULL = national holiday or not yet backfilled.",
    )
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_business_day_substituted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # M1.5 P1 tail — calendar-scope discriminator. Some jurisdictions'
    # "public holidays" differ between tax-filing due-date arithmetic and
    # bank-processing/business-day shifts (e.g. a bank holiday that isn't
    # a gazetted public holiday, or vice versa). NULL = applies to both
    # (unchanged behaviour for every existing row — every current
    # consumer treats every holiday as universally applicable).
    calendar_scope: Mapped[str | None] = mapped_column(
        String(8),
        comment="One of filing / banking / both; NULL = both (unscoped)",
    )
