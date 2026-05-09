"""Factory — pick Remote vs Null based on the active licence, plus
the per-jurisdiction adapter dispatcher (M0 multi-jurisdiction
refactor).

``get_lodgement_service``
    Existing AU-only entry point. Single chokepoint so the gating
    logic lives in one place and tests can
    ``monkeypatch.setattr(LicenseService, "has_feature", ...)``
    to flip between impls without touching the resolver cache.

``get_adapter(jurisdiction, route)``
    M0 entry point. Returns the per-jurisdiction adapter for the
    named jurisdiction, optionally validating that ``route`` is one
    the adapter knows about. AU returns the licence-gated chain;
    NZ/UK/EE return stub adapters that raise ``NotImplementedError``
    keyed to M1/M2/M3.

Both factories are sync — no I/O. The licence snapshot is cached;
new adapters never issue DB calls at construction time.
"""
from __future__ import annotations

from typing import Any

from saebooks.services.licence import LicenseService
from saebooks.services.lodgement.base import LodgementService
from saebooks.services.lodgement.null import NullLodgementService
from saebooks.services.lodgement.remote import RemoteLodgementService

_FEATURE_FLAG = "ato_sbr"


def get_lodgement_service() -> LodgementService:
    """Return the right ``LodgementService`` for the current licence.

    Pro / Enterprise → ``RemoteLodgementService``.
    Anything else → ``NullLodgementService``.
    """
    if LicenseService.has_feature(_FEATURE_FLAG):
        return RemoteLodgementService()
    return NullLodgementService()


# ---------------------------------------------------------------------------
# Per-jurisdiction adapter registry (M0)
# ---------------------------------------------------------------------------


def _au_adapter() -> Any:
    # Local import to avoid cycles — the AU adapter pulls
    # ``get_lodgement_service`` from this module.
    from saebooks.services.lodgement.adapters.au import AULodgementAdapter

    return AULodgementAdapter()


def _nz_adapter() -> Any:
    from saebooks.services.lodgement.adapters.nz import NZLodgementAdapter

    return NZLodgementAdapter()


def _uk_adapter() -> Any:
    from saebooks.services.lodgement.adapters.uk import UKLodgementAdapter

    return UKLodgementAdapter()


def _ee_adapter() -> Any:
    from saebooks.services.lodgement.adapters.ee import EELodgementAdapter

    return EELodgementAdapter()


_ADAPTER_REGISTRY: dict[str, Any] = {
    "AU": _au_adapter,
    "NZ": _nz_adapter,
    "UK": _uk_adapter,
    "EE": _ee_adapter,
}

# Every adapter that exists at M0 supports these route slugs (or
# raises NotImplementedError on dispatch). We surface route validation
# for early-fail rather than waiting for the network call.
_KNOWN_ROUTES_BY_JURISDICTION: dict[str, frozenset[str]] = {
    "AU": frozenset({"stp", "bas", "tpar", "superstream", "abr", "audit"}),
    # NZ/UK/EE: route validation deferred to the per-adapter implementation
    # in M1+. We accept any string and let the adapter raise
    # NotImplementedError on dispatch so the milestone gating message
    # surfaces at the call site.
    "NZ": frozenset(),
    "UK": frozenset(),
    "EE": frozenset(),
}


class UnknownJurisdiction(KeyError):
    """Raised by ``get_adapter`` for a jurisdiction code not in the registry."""


class UnknownRoute(LookupError):
    """Raised by ``get_adapter`` when the named adapter does not own the route."""


def get_adapter(jurisdiction: str, route: str | None = None) -> Any:
    """Return the lodgement adapter for ``jurisdiction``.

    If ``route`` is provided and the adapter knows its route set, the
    factory pre-validates membership so callers fail at dispatch time
    rather than mid-relay. NZ/UK/EE adapters at M0 are stubs and will
    raise ``NotImplementedError`` regardless of the route name —
    route validation there is intentionally lax.

    Raises:
        UnknownJurisdiction: jurisdiction code not registered.
        UnknownRoute: jurisdiction is registered, route is not.
    """
    factory = _ADAPTER_REGISTRY.get(jurisdiction)
    if factory is None:
        raise UnknownJurisdiction(
            f"Unknown jurisdiction {jurisdiction!r}. "
            f"Known: {sorted(_ADAPTER_REGISTRY)}"
        )
    if route is not None:
        known = _KNOWN_ROUTES_BY_JURISDICTION.get(jurisdiction, frozenset())
        if known and route not in known:
            raise UnknownRoute(
                f"{jurisdiction} adapter does not own route {route!r}. "
                f"Known: {sorted(known)}"
            )
    return factory()
