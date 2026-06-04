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

Per-user resolution (launch promo)
----------------------------------

The first-1000-customers launch promo issues a Pro-tier JWT to each
qualifying signup and stamps it on the ``users.launch_promo_jwt``
column (see ``api/v1/signup.py``). Without per-user resolution every
one of those customers would still bind to the process-wide
``settings.edition`` (Community by default for SaaS) and silently
lose the Pro features they were promised.

``resolve_licence_for_user(user)`` is the per-request entry point.
When the user row carries a non-null, signature-verified, non-expired
launch promo JWT, the per-user licence wins; otherwise it falls
through to ``resolve_licence()`` so callers without a user (CLI,
cron jobs, unauth endpoints) keep getting the env-var default.

This *does not* mutate the cached singleton — it composes alongside.
A user with a Pro promo JWT sees Pro features; a different user on
the same box sees whatever the singleton resolved at boot.
"""
from __future__ import annotations

import logging
from threading import RLock
from typing import TYPE_CHECKING

from saebooks.config import settings as _settings
from saebooks.services.licence import jwt as _jwt_driver
from saebooks.services.licence import usb as _usb_driver
from saebooks.services.licence.caps import caps_for
from saebooks.services.licence.models import LicenceSource, ResolvedLicence

if TYPE_CHECKING:
    from saebooks.models.user import User

_log = logging.getLogger(__name__)

_RESOLVED_LICENCE: ResolvedLicence | None = None
_LOCK = RLock()


def resolve_licence(*, force: bool = False) -> ResolvedLicence:
    """Return the process-wide ``ResolvedLicence``, resolving on first call.

    Pass ``force=True`` to re-run resolution (tests; /admin/license
    refresh button). Callers outside of those two paths should not
    need it — the resolver is idempotent-on-boot.
    """
    global _RESOLVED_LICENCE
    with _LOCK:
        if _RESOLVED_LICENCE is not None and not force:
            return _RESOLVED_LICENCE

        resolved = _resolve()
        _RESOLVED_LICENCE = resolved
        return resolved


def resolve_licence_for_user(user: User | None) -> ResolvedLicence:
    """Resolve the effective licence for a given request user.

    Order of preference:

    1. ``user.launch_promo_jwt`` — when present, signature-verified
       (Ed25519 against ``SAEBOOKS_PORTAL_PUBKEY``), and not past its
       grace window. Wins over the process-wide singleton because the
       user was contractually promised Pro for 12 months.
    2. ``resolve_licence()`` — the cached process-wide singleton (env
       var, USB, portal JWT, or community fallback).

    Pass ``user=None`` for non-request callers (CLI, cron jobs, unauth
    endpoints). The behaviour is then identical to ``resolve_licence``.

    A token that fails signature verification or is past the grace
    window does NOT raise — it logs and falls through to the
    singleton, so a corrupted promo JWT can't deny the user the
    baseline tier they would otherwise have.
    """
    if user is None:
        return resolve_licence()

    promo_jwt = getattr(user, "launch_promo_jwt", None)
    if not promo_jwt:
        return resolve_licence()

    try:
        promo = _decode_user_promo_jwt(promo_jwt)
    except Exception:  # defensive — never fail-closed on a corrupt JWT
        _log.exception(
            "launch_promo: unexpected error decoding user promo JWT (user_id=%s)",
            getattr(user, "id", "?"),
        )
        return resolve_licence()

    if promo is None:
        # Decoded as bad signature / expired-past-grace / unknown
        # edition. _decode_user_promo_jwt already logged. Fall back to
        # the singleton so the user sees at least the env-var tier.
        return resolve_licence()

    return promo


def _decode_user_promo_jwt(token: str) -> ResolvedLicence | None:
    """Verify + decode a per-user launch promo JWT.

    Reuses the portal pubkey loader from ``services.licence.jwt`` —
    the license-server signs promo tokens with the same Ed25519 key
    the portal uses for subscription JWTs (see
    ``saebooks-commercial-servers`` infrastructure note). Returns a
    ``ResolvedLicence`` on success, ``None`` on bad signature /
    expired / malformed.

    Test hook: when ``SAEBOOKS_PORTAL_PUBKEY`` is unset (CI / dev),
    this function returns ``None`` for every input — tests must
    monkey-patch ``resolve_licence_for_user`` directly to exercise
    the per-user path. The same pattern as
    ``jwt.build_fake_licence_for_tests``.
    """
    pubkey = _jwt_driver._load_portal_public_key()
    if pubkey is None:
        # No portal pubkey configured — fall through. Logged at INFO
        # only because dev/community installs have no pubkey by design.
        _log.info(
            "launch_promo: SAEBOOKS_PORTAL_PUBKEY unset; user promo JWT ignored",
        )
        return None
    return _jwt_driver._verify_and_decode(token, pubkey)


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
    global _RESOLVED_LICENCE
    with _LOCK:
        _RESOLVED_LICENCE = None
