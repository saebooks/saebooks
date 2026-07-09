"""Master registry of jurisdictions the engine knows how to talk to."""
from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase

# M1.5 · T3 — jurisdiction levels. A jurisdiction is a node in a tree: a
# country ("AU", "US") may own sub-national tax jurisdictions ("AUQ" =
# Queensland, "USC" = California) that levy their own taxes/duties. This is
# what lets the engine represent US federal + state + local sales tax, CA
# federal GST + provincial PST/HST, sub-national VAT, and state stamp duty.
# See docs/multi-jurisdiction.md (M1.5) (T3).
JURISDICTION_LEVELS = ("country", "state", "province", "county", "city")


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

    # ---- T3: multi-level jurisdiction hierarchy (all nullable/defaulted →
    # additive, non-breaking; the 15 existing FKs to jurisdictions.code stay
    # valid, existing rows are country-level with no parent) ----
    parent_code: Mapped[str | None] = mapped_column(
        String(3),
        ForeignKey("jurisdictions.code"),
        comment="Parent jurisdiction (NULL for top-level countries; e.g. AUQ→AU).",
    )
    level: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="country",
        server_default="country",
        comment="Node level: one of JURISDICTION_LEVELS (country|state|province|county|city).",
    )
    iso_subdivision_code: Mapped[str | None] = mapped_column(
        String(6),
        comment="ISO 3166-2 subdivision code where applicable, e.g. 'AU-QLD', 'US-CA'.",
    )
