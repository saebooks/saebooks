"""Landholder / land-rich entity indirect-transfer duty rules (M1.5 · 5-DUTIES).

A per-jurisdiction catalog of the thresholds that make acquiring an
interest IN AN ENTITY dutiable as if the land itself were transferred:
an entity is a "landholder" when its landholdings in the jurisdiction
meet ``landholding_value_threshold``, and an acquisition is "significant"
(and therefore dutiable) at ``significant_interest_pct`` or more —
e.g. 50% of a private company, 90% of a listed entity, 20% of a private
unit trust in Victoria.

``duty_basis`` says how the duty is then computed: at the ordinary
transfer rates on the land-value proportion (``transfer_rates``), or as
a fraction of that amount (``fraction_of_transfer_duty`` with
``basis_fraction``, e.g. 0.1000 for the 10% listed-landholder
concession). Rows are effective-dated; ``effective_to`` NULL = in force.

The postable company-side record is a ``DutiableTransactionEvent`` with
``duty_type='landholder_acquisition'`` — this table is the reference
rule catalog only, no posting-path change.
"""
import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class LandholderEntityClass(enum.StrEnum):
    """Class of entity whose acquisition the rule covers."""

    PRIVATE_COMPANY = "private_company"
    PRIVATE_UNIT_TRUST = "private_unit_trust"
    LISTED_ENTITY = "listed_entity"


LANDHOLDER_ENTITY_CLASSES = tuple(c.value for c in LandholderEntityClass)


class LandholderDutyBasis(enum.StrEnum):
    """How the landholder duty amount is computed once triggered."""

    TRANSFER_RATES = "transfer_rates"  # ordinary rates on the land-value share
    FRACTION_OF_TRANSFER_DUTY = "fraction_of_transfer_duty"  # basis_fraction ×


LANDHOLDER_DUTY_BASES = tuple(b.value for b in LandholderDutyBasis)


class RefLandholderDutyRule(ReferenceBase):
    """One landholder-duty trigger rule for one entity class in one
    (sub-)jurisdiction for a dated period."""

    __tablename__ = "landholder_duty_rules"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "sub_jurisdiction", "entity_class", "effective_from",
            name="uq_landholder_duty_rules_natkey",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    sub_jurisdiction: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment=(
            "Same vocabulary as duty_rate_schedules.state ('QLD', 'NSW', "
            "...); 'ALL' for a country-wide rule."
        ),
    )
    entity_class: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of LANDHOLDER_ENTITY_CLASSES.",
    )
    landholding_value_threshold: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        comment="Entity is a landholder when its in-scope landholdings reach this value.",
    )
    significant_interest_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
        comment="Acquisition percentage that triggers duty (50.00 = 50%).",
    )
    duty_basis: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="One of LANDHOLDER_DUTY_BASES.",
    )
    basis_fraction: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 4),
        comment=(
            "For fraction_of_transfer_duty: the fraction applied (0.1000 = "
            "10% of the duty otherwise payable). NULL for transfer_rates."
        ),
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(
        Date, comment="NULL = still in force."
    )
    description: Mapped[str | None] = mapped_column(String(512))
