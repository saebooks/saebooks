"""Neutral (null-object) tax engine — the "zero jurisdiction modules" floor.

Jurisdiction-module architecture Phase 0 (design doc §3.2,
``~/records/saebooks/jurisdiction-module-architecture-design.md``): a
company whose ``Company.jurisdiction`` is the reserved neutral sentinel
``"XX"`` (or any code with no registered engine, resolved via
``tax_engine.resolve_engine``) must still be able to POST — the ledger
balances, journal lines get a well-formed ``tax_treatment`` snapshot,
and there is simply no GST/VAT determination. This is the null-object
pattern (the template is ``lodgement/null.py``), NOT a circuit-breaker:
deterministic in-memory math has no reachability failure mode, so every
method succeeds with an empty/zero answer instead of refusing or
raising.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from saebooks.services.tax_engine.types import (
    PostingContext,
    TaxTreatment,
    ValidationError,
)

#: Reserved sentinel meaning "no jurisdiction module" (ISO 3166-1
#: user-assigned range; settled decision 3 of the jurisdiction-module
#: architecture). ``Company.jurisdiction = "XX"`` = the bare
#: double-entry accountant with zero bolt-on modules.
NEUTRAL_JURISDICTION = "XX"


class NeutralTaxEngine:
    """Null-object ``TaxEngine`` — records, never computes tax."""

    jurisdiction = NEUTRAL_JURISDICTION

    def compute(self, ctx: PostingContext) -> TaxTreatment:
        """Zero-tax treatment: the full line amount is base, no tax,
        ``direction="none"`` so the line is invisible to every
        BAS/VAT-style reporting bucket."""
        return TaxTreatment(
            jurisdiction=NEUTRAL_JURISDICTION,
            code=ctx.tax_code or "NONE",
            rate=Decimal("0"),
            base=ctx.amount,
            tax=Decimal("0"),
            reporting_type="none",
            direction="none",
        )

    def compute_components(self, ctx: PostingContext) -> list[TaxTreatment]:
        return [self.compute(ctx)]

    def boxes(self, period: Any) -> dict[str, Decimal]:
        """No jurisdiction → no return form → no boxes."""
        return {}

    def validate(self, invoice: Any) -> list[ValidationError]:
        """Nothing jurisdiction-specific to validate."""
        return []
