"""Per-jurisdiction lodgement-adapter registry.

``get_adapter(jurisdiction)`` is the single dispatch point that maps a
jurisdiction code to its adapter. AU returns the fully-wired
:class:`~saebooks.services.lodgement.adapters.au.AULodgementAdapter`
(licence-gated relay chain via the existing factory); UK/EE return
stub adapters whose calls raise ``NotImplementedError`` keyed to the
milestone that lights them up (M2/M3).

Adapters that live inside a jurisdiction package (NZ, so far) are NOT
imported here — the package self-registers via
:func:`register_lodgement_adapter` when
``saebooks.bootstrap.jurisdictions.ensure_loaded()`` imports it (the
Job C registration-inversion shape), and ``get_adapter`` lazily calls
``ensure_loaded()`` on a registry miss before concluding a
jurisdiction is unknown. The remaining static rows are adapters still
housed under ``services/lodgement/adapters/``; each migrates to its
jurisdiction package in a later phase.

Route validation is intentionally applied only to AU — the one wired
jurisdiction. Stub jurisdictions accept any route string at registry
level and raise on call.
"""
from __future__ import annotations

from collections.abc import Callable

from saebooks.services.lodgement.adapters.au import (
    KNOWN_ROUTES as _AU_KNOWN_ROUTES,
)
from saebooks.services.lodgement.adapters.au import (
    AULodgementAdapter,
    UnknownRoute,
)
from saebooks.services.lodgement.adapters.ee import EELodgementAdapter
from saebooks.services.lodgement.adapters.lt import LTLodgementAdapter
from saebooks.services.lodgement.adapters.lv import LVLodgementAdapter
from saebooks.services.lodgement.adapters.uk import UKLodgementAdapter


class UnknownJurisdiction(KeyError):
    """Raised when ``get_adapter`` is asked for a jurisdiction with no adapter."""


# Jurisdiction code -> adapter class (or zero-arg factory returning an
# adapter instance). Static rows for adapters housed in-tree; packaged
# jurisdictions add theirs via ``register_lodgement_adapter``.
_ADAPTERS: dict[str, Callable[[], object]] = {
    "AU": AULodgementAdapter,
    "UK": UKLodgementAdapter,
    "EE": EELodgementAdapter,
    "LT": LTLodgementAdapter,
    "LV": LVLodgementAdapter,
}


def register_lodgement_adapter(
    code: str, adapter: Callable[[], object]
) -> None:
    """Register ``code``'s lodgement adapter (class or zero-arg factory).

    Called by ``saebooks.jurisdictions.<cc>`` packages at import time so
    the core never imports a jurisdiction module. Re-registration
    overwrites (idempotent under repeated package import).
    """
    _ADAPTERS[code] = adapter


def get_adapter(jurisdiction: str, *, route: str | None = None):
    """Return the lodgement adapter for ``jurisdiction``.

    ``route`` is validated only for AU (raises
    :class:`~saebooks.services.lodgement.adapters.au.UnknownRoute` for an
    unknown AU route). Unknown jurisdictions raise
    :class:`UnknownJurisdiction`.
    """
    adapter_cls = _ADAPTERS.get(jurisdiction)
    if adapter_cls is None:
        # Lazy guard (Job C shape): packaged jurisdictions register on
        # import, so make sure the enabled set has been imported before
        # concluding the jurisdiction is unknown.
        from saebooks.bootstrap.jurisdictions import ensure_loaded

        ensure_loaded()
        adapter_cls = _ADAPTERS.get(jurisdiction)
    if adapter_cls is None:
        raise UnknownJurisdiction(
            f"No lodgement adapter for jurisdiction {jurisdiction!r}. "
            f"Known: {sorted(_ADAPTERS)}"
        )

    adapter = adapter_cls()

    if jurisdiction == "AU" and route is not None and route not in _AU_KNOWN_ROUTES:
        raise UnknownRoute(
            f"AU adapter does not support route {route!r}. "
            f"Known: {sorted(_AU_KNOWN_ROUTES)}"
        )

    return adapter
