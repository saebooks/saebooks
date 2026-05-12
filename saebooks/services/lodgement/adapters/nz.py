"""NZ lodgement adapter — stub. Implemented in M1.

Land-targets when M1 lights up:

- ``gst101`` — Goods and Services Tax return (IRD).
- ``ir348``  — Employer Monthly Schedule.
- ``ir3``    — IR3 individual income tax return.
- ``nzbn``   — counterparty lookup (NZBN registry).

The stub raises ``NotImplementedError("NZ lodgement — implemented in M1")``
on every call so the registry-level dispatcher surfaces a uniform
error message regardless of which route was requested.
"""
from __future__ import annotations

from typing import Any, NoReturn


def _stub() -> NoReturn:
    raise NotImplementedError("NZ lodgement — implemented in M1")


class NZLodgementAdapter:
    """Placeholder adapter — every method raises ``NotImplementedError``."""

    jurisdiction: str = "NZ"

    async def lodge(
        self,
        route: str,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> Any:
        _stub()

    async def lookup_nzbn(self, nzbn: str) -> dict[str, Any]:
        _stub()
