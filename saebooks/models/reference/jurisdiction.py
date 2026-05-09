"""Master registry of jurisdictions the engine knows how to talk to."""
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class Jurisdiction(ReferenceBase):
    __tablename__ = "jurisdictions"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    currency_default: Mapped[str] = mapped_column(String(3), nullable=False)
    regulator_name: Mapped[str | None] = mapped_column(String(128))
    regulator_protocol: Mapped[str | None] = mapped_column(
        String(64),
        comment="On-wire protocol identifier (sbr-ebms3, mtd-oauth, oss-portal, e-mta-x-road)",
    )
    decimal_places: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
