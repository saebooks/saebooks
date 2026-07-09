"""Per-jurisdiction capital gains tax (CGT) relief mechanisms (M1.5 · T11).

Before this table the engine had no representation of *how* a jurisdiction
relieves capital gains — AU's 50% discount for assets held > 12 months,
indexation (the older AU method, and used elsewhere), rollover relief on
replacement assets, or a flat exemption. ``dispose_asset()`` only ever
computed a flat ``proceeds - nbv`` gain/loss with no method attached.

This mirrors ``RefTaxCode``: a per-jurisdiction reference table, keyed by
jurisdiction + the relief mechanism in force, active over a date range.
Wiring this into ``dispose_asset()`` / a company-DB ``capital_gain_events``
table is a separate, later change — this table only makes the *reference
data* representable.

See docs/multi-jurisdiction.md (M1.5) (theme T11,
domain "Income, corporate & capital taxes").
"""
import enum
import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class CgtReliefMechanism(enum.StrEnum):
    """Jurisdiction-neutral CGT relief mechanisms. Every local CGT regime
    resolves to exactly one of these so the engine can reason about the
    relief without knowing the local scheme name."""

    DISCOUNT = "discount"        # flat % discount on the gain (AU 50%, held > threshold)
    INDEXATION = "indexation"    # cost base indexed for inflation (CPI-based)
    ROLLOVER = "rollover"        # gain deferred onto a replacement asset
    EXEMPTION = "exemption"      # gain wholly exempt (e.g. main residence)
    NONE = "none"                # no relief — full gain assessable


CGT_RELIEF_MECHANISMS = tuple(m.value for m in CgtReliefMechanism)


class CapitalGainsTaxRegime(ReferenceBase):
    """A CGT relief mechanism in force in one jurisdiction over a date
    range. NOT per-company — asset-disposal services pick a row from here
    keyed by jurisdiction and (later) taxpayer/holding-period facts."""

    __tablename__ = "capital_gains_tax_regimes"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "taxpayer_type", "relief_mechanism", "effective_from",
            name="uq_capital_gains_tax_regimes_jur_tp_mech_eff",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    # Which taxpayer the regime applies to (mirrors IncomeTaxBracket) — the AU
    # 50% CGT discount applies to individuals/trusts but NOT companies, so a
    # relief mechanism must be scoped by taxpayer type. Defaulted for existing
    # rows; 'any' means not taxpayer-scoped.
    taxpayer_type: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="any",
        server_default="any",
        comment="e.g. 'individual_or_trust', 'company', 'any'.",
    )
    relief_mechanism: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="One of CGT_RELIEF_MECHANISMS.",
    )
    relief_rate_or_schedule: Mapped[dict[str, object] | None] = mapped_column(
        JSONB,
        comment=(
            "Flat-rate mechanisms (discount, exemption) store "
            "{'rate_percent': 50.0}; schedule-based mechanisms (indexation) "
            "store the schedule/lookup shape. JSONB so a future indexation "
            "table isn't a schema change."
        ),
    )
    holding_period_threshold_days: Mapped[int | None] = mapped_column(
        Integer,
        comment="Minimum holding period in days to qualify (e.g. 365 for the AU discount). NULL = not holding-period-gated.",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
