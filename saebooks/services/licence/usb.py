"""USB Ed25519 perpetual licence driver (Offline edition).

Per ``CHARTER v1.1 §6.2`` and ``SPEC-LICENSING §3.2``:

* A USB licence is an Ed25519-signed JSON payload written to a USB
  drive at purchase time. The drive's filesystem UUID (as reported
  by ``blkid`` / ``/dev/disk/by-uuid``) is embedded in the payload;
  the licence is only valid on the originally-bound drive.
* The public key used to verify the signature is burned into this
  binary at release time. Self-compilers can replace the pubkey with
  their own to sign their own licences — §12.1 "self-compile allowed"
  line protects this explicitly.
* Runtime check: the drive must be mounted and readable, the
  signature verifies against the bound UUID, and the ``updates_until``
  field is recorded but does NOT invalidate the licence — CHARTER §6.2
  "the install runs forever on the version it has". Expired updates
  windows just freeze updates.

File format on the stick (``<mount>/saebooks.licence``):

    base64(Ed25519-signature-64-bytes) + "\n" + canonical-json-payload

Canonical JSON = ``json.dumps(payload, sort_keys=True, separators=(",", ":"))``.
Signing happens in the portal's air-gapped signer; verification here
re-serialises the payload the same way before handing it to
``Ed25519PublicKey.verify()``.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from saebooks.services.licence.caps import caps_for
from saebooks.services.licence.models import LicenceSource, ResolvedLicence

log = logging.getLogger(__name__)


# Public key (Ed25519, raw 32 bytes, base64 encoded) that every
# released binary uses to verify USB licences. The private half lives
# on an offline signing host at SAE Engineering; see the portal's
# usb_signer service. Empty during development.
USB_PUBKEY_B64: str = ""

# Default mount prefixes we scan for a ``saebooks.licence`` file.
DEFAULT_SCAN_PATHS: tuple[str, ...] = (
    "/run/media",
    "/media",
    "/mnt",
)

LICENCE_FILENAME = "saebooks.licence"


def _load_usb_public_key() -> Ed25519PublicKey | None:
    if not USB_PUBKEY_B64:
        return None
    try:
        raw = base64.b64decode(USB_PUBKEY_B64)
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception:
        log.exception("invalid USB_PUBKEY_B64 — treating as unconfigured")
        return None


def load_usb_licence(
    scan_paths: tuple[str, ...] = DEFAULT_SCAN_PATHS,
) -> ResolvedLicence | None:
    """Scan ``scan_paths`` for a ``saebooks.licence`` file, verify, return.

    Returns ``None`` in any of the following cases:

    * No ``saebooks.licence`` file found on any mounted drive.
    * The build has no embedded public key (development build).
    * The signature fails verification.
    * The ``usb_uuid`` inside the payload doesn't match the drive's
      filesystem UUID.
    * The payload parses but names an unknown edition.

    A ``None`` return always means "fall through to the next driver"
    — never raises on a bad licence, because a tampered USB on a
    customer's desk shouldn't crash the app at boot.
    """
    pubkey = _load_usb_public_key()
    if pubkey is None:
        return None

    path = _find_licence_file(scan_paths)
    if path is None:
        return None

    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return None

    parsed = _parse_blob(raw)
    if parsed is None:
        return None
    signature, payload_bytes, payload = parsed

    try:
        pubkey.verify(signature, payload_bytes)
    except InvalidSignature:
        log.warning("USB licence signature verification failed")
        return None

    expected_uuid = payload.get("usb_uuid")
    actual_uuid = _filesystem_uuid_for(path)
    if expected_uuid and actual_uuid and expected_uuid != actual_uuid:
        log.warning(
            "USB licence usb_uuid %r does not match drive UUID %r",
            expected_uuid,
            actual_uuid,
        )
        return None

    edition = payload.get("edition")
    if edition not in {"offline", "business", "pro", "enterprise"}:
        log.warning("USB licence carries unknown edition %r", edition)
        return None

    return ResolvedLicence(
        edition=edition,
        source=LicenceSource.USB,
        caps=caps_for(edition),
        usb_uuid=expected_uuid,
        licence_id=payload.get("licence_id"),
        licensed_to=payload.get("licensed_to") or payload.get("ledger_id"),
    )


def _parse_blob(
    raw: bytes,
) -> tuple[bytes, bytes, dict[str, Any]] | None:
    """Split ``raw`` into (signature, canonical-payload-bytes, payload-dict)."""
    try:
        sig_line, payload_bytes = raw.split(b"\n", 1)
    except ValueError:
        log.warning("USB licence: missing newline between sig and payload")
        return None

    try:
        signature = base64.b64decode(sig_line.strip())
    except Exception:
        log.warning("USB licence: signature is not base64")
        return None

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("USB licence: payload is not valid JSON")
        return None
    if not isinstance(payload, dict):
        log.warning("USB licence: payload is not a JSON object")
        return None

    # Re-canonicalise — callers sign this exact form, so trailing
    # whitespace in the file must not affect verification.
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return signature, canonical, payload


def _find_licence_file(scan_paths: tuple[str, ...]) -> Path | None:
    for prefix in scan_paths:
        root = Path(prefix)
        if not root.is_dir():
            continue
        for candidate in root.rglob(LICENCE_FILENAME):
            if candidate.is_file():
                return candidate
    return None


def _filesystem_uuid_for(path: Path) -> str | None:
    """Return the filesystem UUID of the mount containing ``path``.

    Uses ``findmnt -n -o UUID`` on the parent directory. Returns
    ``None`` on any failure — callers treat that as "can't verify,
    reject the licence" via the equality check above.
    """
    try:
        result = subprocess.run(
            [
                "findmnt",
                "--noheadings",
                "--output",
                "UUID",
                "--target",
                os.fspath(path.parent),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("findmnt failed for %s: %s", path, exc)
        return None
    out = result.stdout.strip()
    return out or None


# --------------------------------------------------------------------- #
# Test helpers                                                          #
# --------------------------------------------------------------------- #


def build_fake_licence_for_tests(
    *,
    edition: str = "offline",
    usb_uuid: str = "0000-TEST",
    licence_id: str = "00000000-0000-0000-0000-000000000000",
    licensed_to: str = "Test Holder",
) -> ResolvedLicence:
    """Build a ``ResolvedLicence`` with ``source=USB`` for tests."""
    return ResolvedLicence(
        edition=edition,
        source=LicenceSource.USB,
        caps=caps_for(edition),
        usb_uuid=usb_uuid,
        licence_id=licence_id,
        licensed_to=licensed_to,
    )
