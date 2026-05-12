"""UK lodgement adapter — stub. Implemented in M2.

Land-targets when M2 lights up:

- ``vat100``         — UK VAT100 return (HMRC MTD-VAT).
- ``ct600``          — Corporation Tax CT600.
- ``rti_fps``        — PAYE RTI Full Payment Submission.
- ``companies_house``— CRN counterparty lookup.

Every call raises ``NotImplementedError("UK lodgement — implemented in M2")``.
"""
from __future__ import annotations

from typing import Any, NoReturn


def _stub() -> NoReturn:
    raise NotImplementedError("UK lodgement — implemented in M2")


class UKLodgementAdapter:
    """Placeholder adapter — every method raises ``NotImplementedError``."""

    jurisdiction: str = "UK"

    async def lodge(
        self,
        route: str,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> Any:
        _stub()

    async def lookup_crn(self, crn: str) -> dict[str, Any]:
        _stub()
