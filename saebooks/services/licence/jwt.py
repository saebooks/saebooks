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

Implementation uses ``cryptography`` directly rather than pulling
in a JOSE lib — the JWT format is header.payload.signature with
EdDSA (Ed25519) signing over ``header.payload`` bytes. Small enough
to handle inline and avoids the extra dep.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from saebooks.services.licence.caps import EditionCaps, caps_for
from saebooks.services.licence.models import LicenceSource, ResolvedLicence

log = logging.getLogger(__name__)


# Public key (Ed25519) used to verify portal-issued JWTs. Empty in
# development builds; release builds bake in the real portal pubkey
# via ``SAEBOOKS_PORTAL_PUBKEY``.
PORTAL_PUBKEY_B64: str = ""

# Maximum days past ``exp`` we'll still accept a JWT as "read-only".
# CHARTER §6.6: day 31-60 = read-only; past 60, drop to community.
MAX_GRACE_DAYS: int = 60

# File path where the resolver caches the last-fetched JWT. The
# instance re-reads this each boot and refreshes over HTTPS on a
# schedule. Empty string disables the JWT driver entirely.
DEFAULT_JWT_CACHE_PATH: str = "/var/lib/saebooks/licence.jwt"


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + pad)


def _load_portal_public_key() -> Ed25519PublicKey | None:
    if not PORTAL_PUBKEY_B64:
        return None
    try:
        raw = base64.b64decode(PORTAL_PUBKEY_B64)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception:
        log.exception("invalid PORTAL_PUBKEY_B64 — treating as unconfigured")
        return None


def load_portal_jwt(
    cache_path: str = DEFAULT_JWT_CACHE_PATH,
) -> ResolvedLicence | None:
    """Read the cached JWT, verify, return as a ``ResolvedLicence``.

    Returns ``None`` when:

    * The build has no embedded portal pubkey (development build).
    * No JWT is cached yet (fresh install; portal handshake pending).
    * The JWT signature is invalid.
    * The JWT has expired past 60 days past ``exp`` (CHARTER §6.6).

    Returns a licence with ``expires_at`` populated so the resolver
    can decide whether to downgrade writes to read-only or keep
    granting writes. This module does not make that policy decision.
    """
    pubkey = _load_portal_public_key()
    if pubkey is None:
        return None

    path = Path(cache_path)
    if not path.is_file():
        return None

    try:
        token = path.read_text().strip()
    except OSError as exc:
        log.warning("cannot read cached JWT at %s: %s", cache_path, exc)
        return None

    return _verify_and_decode(token, pubkey)


def _verify_and_decode(
    token: str, pubkey: Ed25519PublicKey
) -> ResolvedLicence | None:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        log.warning("malformed JWT: expected 3 segments")
        return None

    try:
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        signature = _b64url_decode(sig_b64)
    except (ValueError, UnicodeDecodeError):
        log.warning("JWT base64/json decode failed")
        return None

    if header.get("alg") != "EdDSA":
        log.warning("JWT alg=%s — expected EdDSA", header.get("alg"))
        return None

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        pubkey.verify(signature, signing_input)
    except InvalidSignature:
        log.warning("JWT signature verification failed")
        return None

    return _payload_to_licence(payload)


def _payload_to_licence(payload: dict[str, Any]) -> ResolvedLicence | None:
    edition = payload.get("edition")
    if edition not in {"business", "pro", "enterprise"}:
        log.warning("JWT carries unknown edition %r", edition)
        return None

    exp = payload.get("exp")
    expires_at = (
        datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None
    )
    if expires_at is not None:
        days_past = (datetime.now(timezone.utc) - expires_at).days
        if days_past > MAX_GRACE_DAYS:
            log.info(
                "JWT expired %d days ago (> %d grace) — falling through",
                days_past,
                MAX_GRACE_DAYS,
            )
            return None

    caps = _caps_from_payload(payload, edition)

    return ResolvedLicence(
        edition=edition,
        source=LicenceSource.JWT,
        caps=caps,
        ledger_id=payload.get("ledger_id"),
        licensed_to=payload.get("licensed_to") or payload.get("ledger_id"),
        expires_at=expires_at,
    )


def _caps_from_payload(
    payload: dict[str, Any], edition: str
) -> EditionCaps:
    """Apply per-subscription cap overrides on top of the edition default.

    Portal bakes per-subscription overrides (Enterprise custom caps,
    Business seat add-ons) into the JWT as ``seat_admin_cap``,
    ``seat_employee_cap``, ``company_cap``. Missing claim → use the
    edition default.
    """
    base = caps_for(edition)
    overrides: dict[str, Any] = {}
    if "seat_admin_cap" in payload:
        overrides["admin_seats"] = payload["seat_admin_cap"]
    if "seat_employee_cap" in payload:
        overrides["employee_seats"] = payload["seat_employee_cap"]
    if "company_cap" in payload:
        overrides["companies"] = payload["company_cap"]
    if not overrides:
        return base
    return dataclasses.replace(base, **overrides)


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
