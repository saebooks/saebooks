"""Master registry of jurisdictions the engine knows how to talk to."""
from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase

# M1.5 · T3 — jurisdiction levels. A jurisdiction is a node in a tree: a
# country ("AUS", "USA") may own sub-national tax jurisdictions that levy
# their own taxes/duties. This is what lets the engine represent US federal
# + state + local sales tax, CA federal GST + provincial PST/HST,
# sub-national VAT, and state stamp duty.
#
# Code vocabulary (M1.5 · 5-SUBJURIS, reference migration 0016): country
# nodes keep their ISO 3166-1 alpha-3 codes ("AUS"); sub-national nodes use
# their ISO 3166-2 code as the primary key ("AU-QLD", "US-CA", "GB-ENG").
# The earlier "AUQ"-style 3-char convention was never seeded and collides
# with the alpha-3 country space ("AUS" = Australia blocks South Australia;
# "AUT" = Austria blocks Tasmania), so the ISO 3166-2 form — globally
# unique by construction and already the ``iso_subdivision_code``
# vocabulary — is the canonical sub-national key.
# See ~/records/saebooks/global-reference-audit-2026-07-09.md (T3).
JURISDICTION_LEVELS = ("country", "state", "province", "county", "city")


class Jurisdiction(ReferenceBase):
    __tablename__ = "jurisdictions"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
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
        String(6),
        ForeignKey("jurisdictions.code"),
        comment="Parent jurisdiction (NULL for top-level countries; e.g. AU-QLD→AUS).",
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
