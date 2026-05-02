"""``LicenseService`` — top-level facade per saebooks-infrastructure.md §8.1.

The infrastructure plan calls for a ``LicenseService.has_feature(name)``
method as the canonical query API. This module is a thin facade over the
two existing primitives:

* ``services.licence.resolver.resolve_licence()`` — the boot-time
  ``ResolvedLicence`` (edition + caps + source).
* ``services.features.is_enabled(flag)`` — the per-flag predicate keyed
  off the active edition.

Code that needs to know whether a paid feature is unlocked should call
``LicenseService.has_feature("bank_feeds")`` rather than reaching into
either primitive directly. That keeps the call-site stable when the
two are reorganised.
"""
from __future__ import annotations

import os
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from saebooks.services import features as _features
from saebooks.services.licence.models import LicenceSource, ResolvedLicence
from saebooks.services.licence.resolver import resolve_licence


# Cache file path mirrored from saebooks.api.v1.license. Kept in sync
# manually (this module must not import from the API package — that
# direction would create a service-↔-route cycle).
_DEFAULT_CACHE_PATH = "/var/lib/saebooks/licence.jwt"


@dataclass(frozen=True, slots=True)
class LicenseSnapshot:
    """Read-only view of the licence as known at boot.

    Surfaced to API responses + the /admin/license page so callers don't
    need to import the resolver internals.
    """

    edition: str
    source: str
    is_paid: bool
    is_perpetual: bool
    expires_at: datetime | None
    licensed_to: str | None
    ledger_id: str | None
    licence_id: str | None


class LicenseService:
    """Facade over resolver + features. Callers use the classmethods."""

    @classmethod
    def has_feature(cls, flag: str) -> bool:
        """Return True iff the active licence enables ``flag``.

        Raises ``ValueError`` for an unknown flag — typoed flag names
        are a programming bug, not a "feature off". This matches
        ``features.is_enabled`` semantics.
        """
        return _features.is_enabled(flag)

    @classmethod
    def edition(cls) -> str:
        """Return the active edition string (community/offline/business/pro/enterprise)."""
        return resolve_licence().edition

    @classmethod
    def snapshot(cls) -> LicenseSnapshot:
        """Return a read-only snapshot of the current licence."""
        rl: ResolvedLicence = resolve_licence()
        return LicenseSnapshot(
            edition=rl.edition,
            source=rl.source.value,
            is_paid=rl.is_paid,
            is_perpetual=rl.is_perpetual,
            expires_at=rl.expires_at,
            licensed_to=rl.licensed_to,
            ledger_id=rl.ledger_id,
            licence_id=rl.licence_id,
        )

    @classmethod
    def reload(cls) -> LicenseSnapshot:
        """Force the resolver to re-read drivers and return the new snapshot.

        Used by ``POST /api/v1/license/refresh`` after the client has
        received a fresh JWT and persisted it to disk.
        """
        resolve_licence(force=True)
        return cls.snapshot()

    @classmethod
    def current_token(cls) -> str | None:
        """Return the raw cached licence JWT string, or None.

        Used by ``RemoteLodgementService`` to populate the
        ``Authorization: Bearer`` header on relay calls to
        ``lodge.saebooks.com.au``. The token is exactly what the
        portal issued; we do not decode or re-sign it here.

        Returns ``None`` when:

        * The cache file does not exist (community / offline / fresh
          install where the portal handshake hasn't happened yet).
        * The cache file exists but is empty / unreadable.

        The caller is expected to check the snapshot's edition before
        even calling this — there's no point trying to lodge STP from
        an unlicensed install. Returning None here is a safety-net,
        not the gating mechanism.
        """
        cache_path = Path(
            os.environ.get("SAEBOOKS_LICENSE_CACHE_PATH", _DEFAULT_CACHE_PATH)
        )
        if not cache_path.is_file():
            return None
        try:
            token = cache_path.read_text().strip()
        except OSError:
            return None
        return token or None
