"""AU lodgement adapter — wraps the existing licence-gated relay chain.

The AU adapter exposes one method per known AU route. Each method
delegates to the same ``LodgementService`` instance returned by the
existing ``saebooks.services.lodgement.factory.get_lodgement_service``
(``RemoteLodgementService`` for licensed Pro / Enterprise, the
``NullLodgementService`` everywhere else).

Routes
------

- ``stp``         -> ``lodge_stp``
- ``bas``         -> ``lodge_bas``
- ``tpar``        -> ``lodge_tpar``
- ``superstream`` -> ``send_superstream``
- ``abr``         -> ``lookup_abr``
- ``audit``       -> ``my_audit_log``

The adapter does not introduce a second cache or licence resolver —
it just routes a (jurisdiction, route) tuple to the right method on
the existing chain. This keeps the legacy callers (``api/v1/lodgement``,
``api/v1/ato_sbr``) working unchanged while giving M1+ jurisdictions
a uniform dispatch shape.
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


class UnknownRoute(LookupError):
    """Raised by the adapter when asked for a route it does not own."""


class AULodgementAdapter:
    """Jurisdiction='AU' adapter over the existing relay chain."""

    jurisdiction: str = "AU"

    def __init__(self, service: LodgementService | None = None) -> None:
        # Default to the licence-aware factory; tests inject a mock
        # ``LodgementService``.
        if service is None:
            from saebooks.services.lodgement.factory import (
                get_lodgement_service,
            )

            service = get_lodgement_service()
        self._service = service

    @property
    def service(self) -> LodgementService:
        """The underlying ``LodgementService`` (Remote or Null)."""
        return self._service

    # ------------------------------------------------------------------ #
    # Per-route dispatch
    # ------------------------------------------------------------------ #

    async def lodge(
        self,
        route: str,
        envelope: bytes,
        idempotency_id: str,
        metadata: dict[str, Any],
    ) -> LodgementResult:
        """Lodge an envelope on one of the four ATO routes.

        ``route`` ∈ {"stp", "bas", "tpar", "superstream"}. Other routes
        raise ``UnknownRoute`` — callers wanting ``abr`` or ``audit``
        use the dedicated methods on the adapter (different signatures).
        """
        if route == "stp":
            return await self._service.lodge_stp(envelope, idempotency_id, metadata)
        if route == "bas":
            return await self._service.lodge_bas(envelope, idempotency_id, metadata)
        if route == "tpar":
            return await self._service.lodge_tpar(envelope, idempotency_id, metadata)
        if route == "superstream":
            return await self._service.send_superstream(envelope, idempotency_id, metadata)
        raise UnknownRoute(
            f"AU adapter does not support lodge route {route!r}. "
            f"Known: {sorted(KNOWN_ROUTES)}"
        )

    async def lookup_abr(self, abn: str) -> dict[str, Any]:
        """ABR lookup — proxied through the relay's quota."""
        return await self._service.lookup_abr(abn)

    async def audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """Pull audit rows the lodge-server holds for this licence."""
        return await self._service.my_audit_log(limit=limit)
