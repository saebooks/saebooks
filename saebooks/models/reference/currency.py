"""ISO-4217 currency registry."""
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class Currency(ReferenceBase):
    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    decimal_places: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    symbol: Mapped[str | None] = mapped_column(String(8))
