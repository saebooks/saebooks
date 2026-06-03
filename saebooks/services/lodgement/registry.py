"""Per-jurisdiction lodgement-adapter registry.

``get_adapter(jurisdiction)`` is the single dispatch point that maps a
jurisdiction code to its adapter. AU returns the fully-wired
:class:`~saebooks.services.lodgement.adapters.au.AULodgementAdapter`
(licence-gated relay chain via the existing factory); NZ/UK/EE return
stub adapters whose calls raise ``NotImplementedError`` keyed to the
milestone that lights them up (M1/M2/M3).

Route validation is intentionally applied only to AU — the one wired
jurisdiction. Stub jurisdictions accept any route string at registry
level and raise on call.
"""
from __future__ import annotations

from saebooks.services.lodgement.adapters.au import (
    KNOWN_ROUTES as _AU_KNOWN_ROUTES,
)
from saebooks.services.lodgement.adapters.au import (
    AULodgementAdapter,
    UnknownRoute,
)
from saebooks.services.lodgement.adapters.ee import EELodgementAdapter
from saebooks.services.lodgement.adapters.nz import NZLodgementAdapter
from saebooks.services.lodgement.adapters.uk import UKLodgementAdapter


class UnknownJurisdiction(KeyError):
    """Raised when ``get_adapter`` is asked for a jurisdiction with no adapter."""


# Jurisdiction code -> adapter class. Add a row when a jurisdiction is wired.
_ADAPTERS: dict[str, type] = {
    "AU": AULodgementAdapter,
    "NZ": NZLodgementAdapter,
    "UK": UKLodgementAdapter,
    "EE": EELodgementAdapter,
}


def get_adapter(jurisdiction: str, *, route: str | None = None):
    """Return the lodgement adapter for ``jurisdiction``.

    ``route`` is validated only for AU (raises
    :class:`~saebooks.services.lodgement.adapters.au.UnknownRoute` for an
    unknown AU route). Unknown jurisdictions raise
    :class:`UnknownJurisdiction`.
    """
    try:
        adapter_cls = _ADAPTERS[jurisdiction]
    except KeyError:
        raise UnknownJurisdiction(
            f"No lodgement adapter for jurisdiction {jurisdiction!r}. "
            f"Known: {sorted(_ADAPTERS)}"
        ) from None

    adapter = adapter_cls()

    if jurisdiction == "AU" and route is not None and route not in _AU_KNOWN_ROUTES:
        raise UnknownRoute(
            f"AU adapter does not support route {route!r}. "
            f"Known: {sorted(_AU_KNOWN_ROUTES)}"
        )

    return adapter
