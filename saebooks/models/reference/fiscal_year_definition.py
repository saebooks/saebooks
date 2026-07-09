"""Fiscal year anchor per jurisdiction."""
from sqlalchemy import ARRAY, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class FiscalYearDefinition(ReferenceBase):
    __tablename__ = "fiscal_year_definitions"

    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), primary_key=True
    )
    fy_start_month: Mapped[int] = mapped_column(Integer, nullable=False)
    fy_start_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    quarter_anchors: Mapped[list[int]] = mapped_column(
        ARRAY(Integer),
        nullable=False,
        comment="Four month-of-year ints marking quarter starts, e.g. [7,10,1,4] for AU",
    )
