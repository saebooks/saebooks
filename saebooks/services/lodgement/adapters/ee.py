"""EE lodgement adapter — stub. Implemented in M3.

Land-targets when M3 lights up:

- ``vat_kmd`` — Estonian VAT (KMD) return (EMTA).
- ``tsd``    — TSD Annex 1 / 2 (Income and Social Tax Declaration).
- ``aps``    — Aktsiaposes (excise) returns.
- ``e_business_register`` — counterparty registry-code lookup.

Every call raises ``NotImplementedError("EE lodgement — implemented in M3")``.
"""
from __future__ import annotations

from typing import Any, NoReturn


def _stub() -> NoReturn:
    raise NotImplementedError("EE lodgement — implemented in M3")


class EELodgementAdapter:
    """Placeholder adapter — every method raises ``NotImplementedError``."""

    jurisdiction: str = "EE"

    async def lodge(
        self,
        route: str,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> Any:
        _stub()

    async def lookup_regcode(self, regcode: str) -> dict[str, Any]:
        _stub()
