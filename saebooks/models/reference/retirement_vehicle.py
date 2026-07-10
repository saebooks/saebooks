"""Per-jurisdiction generic retirement/pension vehicle types (M1.5 · T6).

AU superannuation is modelled today (``saebooks.models.super_fund.SuperFund``)
but that model is APRA/USI/SMSF/ESA-only — there is no way to represent a US
401(k)/IRA, a UK workplace pension, a CA RRSP, or an NZ KiwiSaver account.
This mirrors ``RefEntityStructureType``/``RefTaxCode``: a per-jurisdiction
reference table of the *local* retirement-vehicle names (APRA fund, SMSF,
401(k), Traditional IRA, workplace pension, RRSP, ...) each mapped to a
jurisdiction-neutral ``canonical_bucket`` plus a ``tax_treatment`` code so
services can reason about the vehicle family and its tax consequence
regardless of the local label.

Additive only — ``super_fund``/``employee`` are untouched. A future,
coordinated pass may generalise ``super_fund`` itself onto a
jurisdiction-neutral ``retirement_accounts`` table (see K4 in the audit);
that rename is deliberately out of scope here.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (theme T6, gap K4).
"""
import enum
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class RetirementVehicleBucket(enum.StrEnum):
    """Jurisdiction-neutral retirement-vehicle families. Every local vehicle
    type resolves to exactly one bucket so the engine can reason about
    treatment without knowing the local name."""

    OCCUPATIONAL_PENSION = "occupational_pension"  # employer-sponsored: APRA fund, UK workplace pension, DB scheme
    PERSONAL_PENSION = "personal_pension"           # individually-arranged: IRA, UK personal pension, RRSP
    SELF_DIRECTED = "self_directed"                  # member-controlled: SMSF, US solo 401(k)
    STATE_PENSION = "state_pension"                  # statutory pay-as-you-go: US Social Security, UK State Pension
    DEFINED_BENEFIT = "defined_benefit"               # benefit formula-based, not account-balance-based
    DEFINED_CONTRIBUTION = "defined_contribution"     # account-balance-based (most super, 401(k), RRSP)
    OTHER = "other"


RETIREMENT_VEHICLE_BUCKETS = tuple(b.value for b in RetirementVehicleBucket)


class RetirementTaxTreatment(enum.StrEnum):
    """Where the tax is applied across contribution / growth / withdrawal.

    Standard pension-taxation shorthand: E = exempt, T = taxed, in
    contribution/earnings/withdrawal order. AU super and US 401(k)/UK
    workplace pensions are EET (contributions and growth are tax-favoured,
    withdrawals taxed); Roth IRA/TFSA-style vehicles are TEE (contributed
    from after-tax money, tax-free growth and withdrawal).
    """

    EET = "EET"    # exempt contributions, exempt growth, taxed withdrawal (AU super, traditional 401(k)/IRA)
    TEE = "TEE"    # taxed contributions, exempt growth, exempt withdrawal (Roth IRA/401(k))
    ETT = "ETT"    # exempt contributions, taxed growth, taxed withdrawal (uncommon; some DB schemes)
    OTHER = "other"


RETIREMENT_TAX_TREATMENTS = tuple(t.value for t in RetirementTaxTreatment)


class RefRetirementVehicleType(ReferenceBase):
    """A local retirement/pension vehicle type in one jurisdiction, mapped to
    a canonical bucket + tax treatment. NOT per-company — companies/employees
    pick a code from here (AU today via ``super_fund``, generically once K4's
    coordinated generalisation lands)."""

    __tablename__ = "retirement_vehicle_types"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "code",
            name="uq_retirement_vehicle_types_jur_code",
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
        comment="Stable per-jurisdiction code, e.g. 'apra_super', 'smsf', 'us_401k'.",
    )
    local_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Local label, e.g. 'APRA-Regulated Superannuation Fund', '401(k) Plan'.",
    )
    canonical_bucket: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of RETIREMENT_VEHICLE_BUCKETS — the jurisdiction-neutral family.",
    )
    tax_treatment: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="One of RETIREMENT_TAX_TREATMENTS — EET/TEE/ETT/other.",
    )
    description: Mapped[str | None] = mapped_column(String(512))
