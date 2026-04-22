"""Portal JWT subscription licence driver (Business / Pro / Enterprise).

Per ``CHARTER v1.1 §6.6`` + ``SPEC-LICENSING §3.3``:

* A subscription licence is a short-lived (24-72h) JWT issued by the
  SAE Books portal, signed with the portal's Ed25519 key. The token
  carries the subscription's edition, ledger-identifier, seat caps,
  and expiry. The instance refreshes the token from the portal over
  HTTPS every 12h; the grace-period state machine lives there too.
* The embedded public key in the binary verifies token signatures.
  No per-token network round-trip is required (and no phone-home
  fingerprint is sent — only the token itself is fetched).
* Grace behaviour: day 0-14 = normal; 15-30 = persistent banner;
  31-60 = read-only (write routes return 402); 61+ = drop to
  community. The state machine runs in the resolver loop; this
  module only *verifies* a JWT that the resolver hands it.

This module is a scaffolded interface. Real JWT verification (PyJWT
or the ``authlib`` JOSE module) lands alongside the portal in Wave 5;
until then ``load_portal_jwt`` returns ``None`` on every call so the
resolver falls through to community.
"""
from __future__ import annotations

from saebooks.services.licence.caps import caps_for
from saebooks.services.licence.models import LicenceSource, ResolvedLicence


# Public key (Ed25519) used to verify portal-issued JWTs. Empty in
# development builds; release builds bake in the real portal pubkey
# via ``SAEBOOKS_PORTAL_PUBKEY``.
PORTAL_PUBKEY_B64: str = ""

# File path where the resolver caches the last-fetched JWT. The
# instance re-reads this each boot and refreshes over HTTPS on a
# schedule. Empty string disables the JWT driver entirely.
DEFAULT_JWT_CACHE_PATH: str = "/var/lib/saebooks/licence.jwt"


def load_portal_jwt(cache_path: str = DEFAULT_JWT_CACHE_PATH) -> ResolvedLicence | None:
    """Read the cached JWT, verify, return as a ``ResolvedLicence``.

    Returns ``None`` when:

    * The build has no embedded portal pubkey (development build).
    * No JWT is cached yet (fresh install; portal handshake pending).
    * The JWT signature is invalid.
    * The JWT has expired past the grace period (60+ days).

    Non-``None`` returns may include an ``expires_at`` deep into the
    grace period; the caller (the resolver) inspects that value to
    decide whether to downgrade writes to read-only or drop to
    community. This module does not make that policy decision.
    """
    if not PORTAL_PUBKEY_B64:
        return None
    # Real implementation lands in Wave 5 alongside the portal.
    return None


# --------------------------------------------------------------------- #
# Test helpers                                                          #
# --------------------------------------------------------------------- #


def build_fake_licence_for_tests(
    *,
    edition: str = "business",
    ledger_id: str = "test-ledger",
    licensed_to: str = "Test Holder",
) -> ResolvedLicence:
    """Build a ``ResolvedLicence`` with ``source=JWT`` for tests.

    Bypasses every crypto check. Only call from tests.
    """
    return ResolvedLicence(
        edition=edition,
        source=LicenceSource.JWT,
        caps=caps_for(edition),
        ledger_id=ledger_id,
        licensed_to=licensed_to,
    )
