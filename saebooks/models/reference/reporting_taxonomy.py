"""Per-jurisdiction regulatory e-filing / financial-statement taxonomies
(M1.5 · T10b).

Registry of the taxonomies regulators accept electronic financial reports
in — XBRL-family taxonomies (AU SBR, UK FRC/iXBRL, EE XBRL GL, SG ACRA,
IN MCA) and their non-XBRL equivalents. This gives accounts and reports a
canonical vocabulary to be tagged AGAINST; the per-account element mapping
(one account → many taxonomy elements) is a company-side m2m deliberately
deferred until it has a consumer (see the audit's account_taxonomy_mapping
proposal).

AU parity: the live SBR lodgement path (``services/lodgement/sbr/xbrl.py``)
already renders XBRL instances under the SBR AU taxonomy — the AU seed row
here names that same taxonomy, so registering it changes no behaviour.

See ~/records/saebooks/global-reference-audit-2026-07-09.md
(chart-of-accounts domain, "Statutory financial-statement line /
regulatory e-filing taxonomy tag").
"""
import enum
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class TaxonomyFormat(enum.StrEnum):
    """Serialisation family a taxonomy's filings are rendered in."""

    XBRL = "xbrl"          # classic XBRL 2.1 instances (AU SBR, IN MCA)
    IXBRL = "ixbrl"        # inline XBRL in XHTML (UK HMRC/FRC, EU ESEF)
    XBRL_GL = "xbrl_gl"    # XBRL Global Ledger (EE KMD3 feedback)
    XML = "xml"            # bespoke XML schemas (SAF-T and kin)
    JSON = "json"          # JSON-based filing schemas
    OTHER = "other"


TAXONOMY_FORMATS = tuple(f.value for f in TaxonomyFormat)


class RefReportingTaxonomy(ReferenceBase):
    """One regulator-published reporting taxonomy in one jurisdiction.
    NOT per-company — mappings reference a row by code."""

    __tablename__ = "reporting_taxonomies"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "code",
            name="uq_reporting_taxonomies_jur_code",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Stable per-jurisdiction code, e.g. 'sbr_au', 'frc_uk', 'esef'.",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    taxonomy_format: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="One of TAXONOMY_FORMATS — the serialisation family.",
    )
    authority: Mapped[str | None] = mapped_column(
        String(128),
        comment="Publishing regulator, e.g. 'Australian Taxation Office / Treasury (SBR)'.",
    )
    version: Mapped[str | None] = mapped_column(
        String(64), comment="Taxonomy release where versioned, e.g. '2026.06'."
    )
    description: Mapped[str | None] = mapped_column(String(512))
