"""ISO-3166 country registry with EU/EEA/OSS membership flags."""
from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class Country(ReferenceBase):
    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    code_alpha2: Mapped[str] = mapped_column(String(2), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    currency_default: Mapped[str | None] = mapped_column(String(3))
    in_eu: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    in_eea: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    in_oss: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Eligible for One Stop Shop VAT scheme",
    )
