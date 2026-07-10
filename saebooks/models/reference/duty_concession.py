"""Duty concession / exemption reference catalog (M1.5 · T5).

A per-jurisdiction catalog of stamp/transfer/conveyance duty concessions
and exemptions (first-home buyer, off-the-plan, primary-production
transfer, ...), mirroring ``RefEntityStructureType``: a per-jurisdiction
reference table, NOT company-scoped, that a company-DB row references by
id (``DutiableTransactionEvent.applied_concession_id`` — opaque, non-FK;
the reference DB is a separate database, see that model's docstring).

This is the reference-catalog slice of the concession concept only
(jurisdiction, code, name, relief_type, rate_or_amount) — the audit's
proposed clawback state machine (post-settlement clawback on a
concession that turns out not to have been met) is a company-scoped
follow-on, deferred out of this theme.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T5).
"""
import enum
import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class DutyReliefType(enum.StrEnum):
    """How the concession reduces the assessed duty."""

    FULL_EXEMPTION = "full_exemption"      # duty reduced to zero
    RATE_REDUCTION = "rate_reduction"      # a lower rate applies
    FIXED_REBATE = "fixed_rebate"          # a fixed amount is deducted
    THRESHOLD_ABATEMENT = "threshold_abatement"  # no duty below a value threshold


DUTY_RELIEF_TYPES = tuple(t.value for t in DutyReliefType)


class RefDutyConcession(ReferenceBase):
    """One local duty concession/exemption in one jurisdiction.

    NOT per-company — a ``DutiableTransactionEvent`` references a row
    from here by id (opaque, no cross-DB FK).
    """

    __tablename__ = "duty_concessions"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "code",
            name="uq_duty_concessions_jur_code",
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
        comment="Stable per-jurisdiction code, e.g. 'first_home_concession'.",
    )
    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Local label, e.g. 'First Home Concession'.",
    )
    relief_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of DUTY_RELIEF_TYPES.",
    )
    rate_or_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 4),
        nullable=False,
        comment=(
            "A rate (fraction) for rate_reduction, a fixed amount for "
            "fixed_rebate/threshold_abatement, or 0 for full_exemption."
        ),
    )
    description: Mapped[str | None] = mapped_column(String(512))
