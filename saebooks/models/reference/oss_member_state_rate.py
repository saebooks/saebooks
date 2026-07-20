"""Per-EU-member-state standard VAT rate for the Union OSS scheme.

See ``alembic_reference/versions/0011_oss_member_state_rates.py`` for the
scope note on which member states are (and are not yet) covered, and
``saebooks.services.lodgement.oss_q.generator`` for how this table is
consumed (destination-country rate lookup for the member-state x rate
aggregation).
"""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class OssMemberStateRate(ReferenceBase):
    """One EU member state's standard VAT rate, dated (natural key:
    ``country_code`` + ``effective_from``, mirroring ``tax_codes.yaml``'s
    dated-rate-series convention)."""

    __tablename__ = "oss_member_state_rates"
    __table_args__ = (
        UniqueConstraint(
            "country_code", "effective_from",
            name="uq_oss_member_state_rates_country_eff",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    country_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("countries.code"), nullable=False,
        comment="ISO 3166-1 alpha-3 — the OSS destination (consumption) member state.",
    )
    standard_vat_rate_percent: Mapped[Decimal] = mapped_column(
        Numeric(7, 4),
        nullable=False,
        comment="Standard VAT rate as a percentage (21.0000 = 21%, not 0.21).",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, comment="NULL = still in force.")
    source_note: Mapped[str | None] = mapped_column(String(256))
