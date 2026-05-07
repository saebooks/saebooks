"""Top-level licence resolver — orchestrates USB → JWT → community.

Called once at app boot from ``saebooks.main`` (wired in Wave 5 when
the drivers go live). Returns a ``ResolvedLicence`` that the rest of
the app reads via ``settings.edition`` and the cached
``_RESOLVED_LICENCE`` singleton below.

Order
-----

1. Try ``usb.load_usb_licence`` — perpetual takes precedence over
   subscription because a customer who paid once-off for Offline
   should not silently switch to Business features if a JWT happens
   to be cached on the same box.
2. Try ``jwt.load_portal_jwt`` — subscription path for Business /
   Pro / Enterprise.
3. Fall through to community. Always succeeds; no licence required.

An explicit override via ``SAEBOOKS_EDITION`` env var short-circuits
all three — it's the developer / test-harness escape hatch. The
resolver honours it but logs a warning so a production box with the
env accidentally set doesn't silently lose enforcement.
"""
from __future__ import annotations

import logging
from threading import RLock

from saebooks.config import settings as _settings
from saebooks.services.licence import jwt as _jwt_driver
from saebooks.services.licence import usb as _usb_driver
from saebooks.services.licence.caps import caps_for
from saebooks.services.licence.models import LicenceSource, ResolvedLicence

_log = logging.getLogger(__name__)

_RESOLVED_LICENCE: ResolvedLicence | None = None
_LOCK = RLock()


def resolve_licence(*, force: bool = False) -> ResolvedLicence:
    """Return the process-wide ``ResolvedLicence``, resolving on first call.

    Pass ``force=True`` to re-run resolution (tests; /admin/license
    refresh button). Callers outside of those two paths should not
    need it — the resolver is idempotent-on-boot.
    """
    global _RESOLVED_LICENCE  # noqa: PLW0603
    with _LOCK:
        if _RESOLVED_LICENCE is not None and not force:
            return _RESOLVED_LICENCE

        resolved = _resolve()
        _RESOLVED_LICENCE = resolved
        return resolved


def _resolve() -> ResolvedLicence:
    # Developer override. Logged loudly so a misconfigured prod box is
    # obvious at boot.
    env_edition = _settings.edition
    if env_edition != "community":
        _log.warning(
            "SAEBOOKS_EDITION override active: edition=%s (licence drivers skipped)",
            env_edition,
        )
        return ResolvedLicence(
            edition=env_edition,
            source=LicenceSource.COMMUNITY_FALLBACK,
            caps=caps_for(env_edition),
        )

    usb_result = _usb_driver.load_usb_licence()
    if usb_result is not None:
        _log.info(
            "USB licence accepted: edition=%s licence_id=%s",
            usb_result.edition,
            usb_result.licence_id,
        )
        return usb_result

    jwt_result = _jwt_driver.load_portal_jwt()
    if jwt_result is not None:
        _log.info(
            "Portal JWT accepted: edition=%s ledger_id=%s expires=%s",
            jwt_result.edition,
            jwt_result.ledger_id,
            jwt_result.expires_at,
        )
        return jwt_result

    _log.info("No licence found — running Community edition")
    return ResolvedLicence(
        edition="community",
        source=LicenceSource.COMMUNITY_FALLBACK,
        caps=caps_for("community"),
    )


def _reset_for_tests() -> None:
    """Clear the cached resolution. Test-only."""
    global _RESOLVED_LICENCE  # noqa: PLW0603
    with _LOCK:
        _RESOLVED_LICENCE = None
