"""Data shapes returned by the licence resolver.

``ResolvedLicence`` is what the resolver gives back after validating a
licence (or falling through to community). The rest of the app —
``settings.edition``, seat enforcement, /admin/license rendering —
reads from here.

Licence source tracking
-----------------------

``source`` tells callers *how* the current edition was decided. This
matters for the /admin/license page (so we can show "USB licence ABC-
123 detected on /dev/sdX" vs "portal JWT valid until 2027-01-01") and
for expiry nudges (subscription licences show grace-period banners at
day-15 / day-30 / day-60 per SPEC-LICENSING §6; perpetual USB licences
don't expire at all).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from saebooks.services.licence.caps import EditionCaps


class LicenceSource(str, Enum):
    """How the current edition was resolved at boot."""

    COMMUNITY_FALLBACK = "community_fallback"
    USB = "usb"
    JWT = "jwt"


@dataclass(frozen=True, slots=True)
class ResolvedLicence:
    """Result of licence resolution.

    Emitted once at boot by ``resolver.resolve_licence`` and cached.
    """

    edition: str
    source: LicenceSource
    caps: EditionCaps

    # USB drivers set ``usb_uuid`` (hardware serial) + ``licence_id``
    # (UUID embedded in the signed payload). JWT drivers set
    # ``ledger_id`` (customer-chosen identifier bound to the sub).
    usb_uuid: str | None = None
    licence_id: str | None = None
    ledger_id: str | None = None

    # Perpetual USB licences set ``updates_until`` (12-month updates
    # window from purchase); subscription licences set ``expires_at``
    # (end of current billing period). Both are optional.
    expires_at: datetime | None = None
    updates_until: datetime | None = None

    # Legal entity the licence is issued to — only meaningful for paid
    # editions. Shown on /admin/license as "Licensed to: <name>".
    licensed_to: str | None = None

    @property
    def is_perpetual(self) -> bool:
        """Once-off purchases (Offline USB) never expire."""
        return self.source is LicenceSource.USB

    @property
    def is_paid(self) -> bool:
        """Anything above community is a paid licence."""
        return self.source is not LicenceSource.COMMUNITY_FALLBACK
