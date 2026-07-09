"""Per-jurisdiction legal-entity / business-structure types (M1.5 · T4).

The engine must be able to say *what kind of legal entity* a set of books
belongs to — because structure drives accounting and tax treatment (trust
distributions, partner capital accounts, corporation-tax vs pass-through,
franking eligibility, super/pension-fund rules). Before this table the
``companies`` row had ``abn``/``acn`` but no structure field at all, so the
engine could not distinguish a Pty Ltd from a discretionary trust from an
SMSF — let alone a US LLC / C-corp / S-corp, a UK LLP, or a pension plan.

This mirrors ``RefTaxCode``: a per-jurisdiction reference table of the
*local* structure names (Pty Ltd, LLC, GmbH, ...) each mapped to a
jurisdiction-neutral ``canonical_bucket`` so services can reason about the
structure family regardless of the local label. A company references one
of these by ``companies.entity_structure_code`` (validated at the service
layer against the company's own jurisdiction — no cross-DB FK, same pattern
as ``companies.jurisdiction``).

See docs/multi-jurisdiction.md (M1.5) (theme T4).
"""
import enum
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class EntityStructureBucket(enum.StrEnum):
    """Jurisdiction-neutral structure families. Every local structure type
    resolves to exactly one bucket so the engine can reason about treatment
    without knowing the local name."""

    SOLE_TRADER = "sole_trader"          # sole trader / sole proprietor / Einzelunternehmen
    PARTNERSHIP = "partnership"          # general/limited partnership, LP, LLP
    COMPANY_LIMITED = "company_limited"  # Pty Ltd / Ltd / Inc / C-corp / GmbH / SA
    PASS_THROUGH = "pass_through"        # US LLC / S-corp — corporate form, pass-through tax
    TRUST = "trust"                      # discretionary / unit / hybrid / fixed trust
    PENSION_FUND = "pension_fund"        # super fund / SMSF / 401(k)/IRA plan / workplace pension
    NONPROFIT = "nonprofit"              # association / charity / 501(c) / CIC
    COOPERATIVE = "cooperative"          # co-op / mutual
    GOVERNMENT = "government"            # government / statutory body
    OTHER = "other"


ENTITY_STRUCTURE_BUCKETS = tuple(b.value for b in EntityStructureBucket)


class RefEntityStructureType(ReferenceBase):
    """A local business-structure type in one jurisdiction, mapped to a
    canonical bucket. NOT per-company — companies pick a code from here."""

    __tablename__ = "entity_structure_types"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "code",
            name="uq_entity_structure_types_jur_code",
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
        comment="Stable per-jurisdiction code, e.g. 'pty_ltd', 'disc_trust', 'smsf'.",
    )
    local_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Local label, e.g. 'Proprietary Limited Company', 'Discretionary Trust'.",
    )
    canonical_bucket: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of ENTITY_STRUCTURE_BUCKETS — the jurisdiction-neutral family.",
    )
    description: Mapped[str | None] = mapped_column(String(512))
