"""Community licence resolver — PUBLIC SHIM (commercial seam stubbed).

The private build resolves a paid licence from a signed USB stick (perpetual
Offline) or a portal-issued subscription JWT (Business / Pro / Enterprise). That
validation is the commercial control plane and is NOT shipped in the open repo.

This community shim keeps the exact public surface the engine imports —
``resolve_licence`` / ``resolve_licence_for_user`` / ``_reset_for_tests`` and
the module-level ``_settings`` / ``_RESOLVED_LICENCE`` / ``_LOCK`` that the test
harness rebinds — but:

* drops the USB (Ed25519) and portal-JWT drivers entirely (no paid validation);
* still honours an explicit ``SAEBOOKS_EDITION`` override. On an AGPL self-host
  this is how the operator selects their edition (CHARTER §12.1 self-compile /
  self-run is allowed); paid-tier *unlock as a service* is the commercial
  licence, which lives in the private build. With no override set, the engine
  runs Community.
* ``resolve_licence_for_user`` ignores the per-user launch-promo JWT (a
  portal-signed commercial artefact) and returns the process-wide resolution.

The seat/company caps in ``caps.py`` and the feature-flag model in
``services.features`` are unchanged and public — the edition still gates
features; this shim only removes the commercial *validation of entitlement*.
"""
from __future__ import annotations

import logging
from threading import RLock
from typing import TYPE_CHECKING

from saebooks.config import settings as _settings
from saebooks.services.licence.caps import caps_for
from saebooks.services.licence.models import LicenceSource, ResolvedLicence

if TYPE_CHECKING:
    from saebooks.models.user import User

_log = logging.getLogger(__name__)

#: Marks this module as the open-engine PUBLIC SHIM (commercial licence/launch-promo
#: control-plane stubbed out). The private build's real ``resolver`` never defines
#: this, so tests can ``skipif(getattr(resolver, "__OPEN_ENGINE_STUB__", False), ...)``
#: to auto-skip commercial-only assertions in the open tree while still running them
#: (unchanged) in the private build.
__OPEN_ENGINE_STUB__ = True

_RESOLVED_LICENCE: ResolvedLicence | None = None
_LOCK = RLock()


def resolve_licence(*, force: bool = False) -> ResolvedLicence:
    """Return the process-wide ``ResolvedLicence``, resolving on first call.

    Pass ``force=True`` to re-run resolution (tests; /admin/license refresh).
    """
    global _RESOLVED_LICENCE
    with _LOCK:
        if _RESOLVED_LICENCE is not None and not force:
            return _RESOLVED_LICENCE
        resolved = _resolve()
        _RESOLVED_LICENCE = resolved
        return resolved


def resolve_licence_for_user(user: User | None) -> ResolvedLicence:
    """Resolve the effective licence for a request user.

    Community build: there is no portal-signed per-user launch-promo JWT to
    verify, so this always returns the process-wide resolution. The signature
    is preserved so ``services.features`` and the API can call it unchanged.
    """
    return resolve_licence()


def _resolve() -> ResolvedLicence:
    edition = _settings.edition
    if edition != "community":
        # Operator-selected edition on a self-host (or the test harness).
        _log.info("SAEBOOKS_EDITION override active: edition=%s", edition)
        return ResolvedLicence(
            edition=edition,
            source=LicenceSource.COMMUNITY_FALLBACK,
            caps=caps_for(edition),
        )
    return ResolvedLicence(
        edition="community",
        source=LicenceSource.COMMUNITY_FALLBACK,
        caps=caps_for("community"),
    )


def _reset_for_tests() -> None:
    """Clear the cached resolution. Test-only."""
    global _RESOLVED_LICENCE
    with _LOCK:
        _RESOLVED_LICENCE = None
