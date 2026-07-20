"""Per-jurisdiction statutory chart-of-accounts frameworks (M1.5 · T10b).

Many jurisdictions legally mandate (or strongly standardise) the numbering
plan a company's chart of accounts must follow — Germany's SKR03/SKR04,
France's Plan Comptable Général, Estonia's standard chart, Spain's PGC.
Australia mandates none: any AU row here carries
``is_legally_mandated=false`` and the engine's recommended
``chart_template`` rows remain a convention, not a legal requirement.

This is the registry of those frameworks. A company opts into one via the
nullable ``companies.statutory_framework_code`` column (validated at the
service layer against the company's own jurisdiction — no cross-DB FK,
same pattern as ``companies.jurisdiction`` / ``entity_structure_code``),
and ``chart_template`` rows may scope themselves to a framework via their
own nullable ``statutory_framework_code`` column.

See ~/records/saebooks/global-reference-audit-2026-07-09.md
(chart-of-accounts domain, "Statutory chart-of-accounts framework
identity").
"""
import uuid

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class RefStatutoryAccountFramework(ReferenceBase):
    """A named chart-of-accounts framework in one jurisdiction (SKR03,
    PCG, ...). NOT per-company — companies reference a row by code."""

    __tablename__ = "statutory_account_frameworks"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "code",
            name="uq_statutory_account_frameworks_jur_code",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Stable per-jurisdiction code, e.g. 'skr03', 'pcg', 'general'.",
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_legally_mandated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment=(
            "True when the jurisdiction legally requires this numbering "
            "plan; false for recommended/conventional charts (all of AU)."
        ),
    )
    mandating_authority: Mapped[str | None] = mapped_column(
        String(128),
        comment="Body behind the framework, e.g. 'DATEV', 'Autorité des normes comptables'.",
    )
    version: Mapped[str | None] = mapped_column(
        String(32), comment="Framework edition/year where versioned, e.g. '2026'."
    )
    description: Mapped[str | None] = mapped_column(String(512))
