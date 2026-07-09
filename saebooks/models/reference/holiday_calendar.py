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
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_business_day_substituted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
