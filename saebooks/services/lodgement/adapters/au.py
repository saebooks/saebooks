"""AU lodgement adapter — PUBLIC SHIM (certified transmission stubbed).

The private build routes the AU submit paths (STP / BAS / TPAR / SuperStream)
plus the relay-quota ABR lookup + audit through the licence-gated
``RemoteLodgementService`` chain to SAE Books' certified lodge-server. That
certified transmission is a commercial feature and is NOT shipped in the open
repo.

Symbols preserved exactly (the registry imports ``KNOWN_ROUTES`` /
``AULodgementAdapter`` / ``UnknownRoute``; the adapter-registry test asserts the
factory wiring on ``.service``). The constructor still selects the licence-aware
service via the factory (so ``.service`` is Remote/Null as before), but every
*call* raises ``NotImplementedError("commercial feature")`` — the open engine
computes returns but does not transmit them.
"""
from __future__ import annotations

from typing import Any

from saebooks.services.lodgement.base import LodgementResult, LodgementService

KNOWN_ROUTES: frozenset[str] = frozenset({
    "stp",
    "bas",
    "tpar",
    "superstream",
    "abr",
    "audit",
})

_COMMERCIAL = (
    "commercial feature: certified ATO transmission is not available in the "
    "open engine — the community build computes and generates the return file "
    "but does not lodge it"
)


class UnknownRoute(LookupError):
    """Raised by the adapter when asked for a route it does not own."""


class AULodgementAdapter:
    """Jurisdiction='AU' adapter — submit paths stubbed in the open engine."""

    jurisdiction: str = "AU"

    def __init__(self, service: LodgementService | None = None) -> None:
        if service is None:
            from saebooks.services.lodgement.factory import get_lodgement_service

            service = get_lodgement_service()
        self._service = service

    @property
    def service(self) -> LodgementService:
        """The underlying ``LodgementService`` (Remote or Null)."""
        return self._service

    async def lodge(
        self,
        route: str,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        if route not in KNOWN_ROUTES:
            raise UnknownRoute(
                f"AU adapter does not support lodge route {route!r}. "
                f"Known: {sorted(KNOWN_ROUTES)}"
            )
        raise NotImplementedError(_COMMERCIAL)

    async def lookup_abr(self, abn: str) -> dict[str, Any]:
        raise NotImplementedError(_COMMERCIAL)

    async def audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        raise NotImplementedError(_COMMERCIAL)
