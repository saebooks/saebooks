"""Per-jurisdiction capital-gains event-type catalogue (M1.5 · Wave 5-Income).

``capital_gains_tax_regimes`` (T11) records *how* a jurisdiction relieves
a gain; nothing recorded *which statutory event* crystallised it. AU's
CGT operates entirely off a statutory event catalogue (ITAA 1997 s 104-5:
A1 disposal, C1 loss/destruction, D2 granting an option, K7 depreciating
-asset balancing adjustment, ...), and the event determines timing, the
gain/loss formula and discount eligibility. Other jurisdictions have
smaller but analogous taxonomies (e.g. deemed-disposal events).

This is the reference *catalogue* of those event types. Wiring
``dispose_asset()`` / a company-DB ``capital_gain_events`` transaction
table to it is a separate, later change — this table only makes the
reference data representable (same posture as ``CapitalGainsTaxRegime``).
The catalogue carries no effective dating: events are added/repealed by
statute rarely, and the date-ranged mechanics live on the regime table.

See ~/records/saebooks/global-reference-audit-2026-07-09.md (domain
"Income, corporate & capital taxes", "Capital gains tax (CGT) treatment").
"""
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from saebooks.db import ReferenceBase


class RefCapitalGainsEventType(ReferenceBase):
    """One statutory capital-gains event type in one jurisdiction (AU
    'A1', 'C2', ...). NOT per-company — disposal/valuation services pick
    a row from here keyed by jurisdiction + event code."""

    __tablename__ = "capital_gains_event_types"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction", "code",
            name="uq_capital_gains_event_types_jur_code",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(3), ForeignKey("jurisdictions.code"), nullable=False
    )
    code: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="Statutory event code, e.g. AU 'A1', 'C2', 'K7'.",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment=(
            "Jurisdiction's statutory grouping of the event, e.g. AU "
            "'disposals', 'trusts', 'leases', 'consolidation'."
        ),
    )
    statutory_reference: Mapped[str | None] = mapped_column(
        String(64),
        comment="Provision defining the event, e.g. 'ITAA 1997 s 104-10'.",
    )
    description: Mapped[str | None] = mapped_column(String(512))
