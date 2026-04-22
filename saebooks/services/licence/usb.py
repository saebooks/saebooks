"""USB Ed25519 perpetual licence driver (Offline edition).

Per ``CHARTER v1.1 §6.2`` and ``SPEC-LICENSING §3.2``:

* A USB licence is an Ed25519-signed JSON payload written to a USB
  drive at purchase time. The drive's hardware UUID (``ID_SERIAL`` as
  reported by udev) is embedded in the payload; the licence is only
  valid on the originally-bound drive.
* The public key used to verify the signature is burned into this
  binary at release time. Self-compilers can replace the pubkey with
  their own to sign their own licences — §12.1 "self-compile allowed"
  line protects this explicitly.
* Runtime check: the drive must be mounted and readable, the signature
  verifies against the bound UUID, and the ``updates_until`` field has
  not elapsed *if we are inside the licence's update window*. Expired
  update windows don't disable the licence — they just freeze updates
  (CHARTER §6.2 "the install runs forever on the version it has").

Activation (first-use online handshake) is the portal's concern
(``saebooks-portal.usb_activation``) — this module only handles
*reading* an already-signed licence blob and verifying it.

This module is a scaffolded interface. Real Ed25519 verification
arrives alongside the portal in Wave 5; until then ``load_usb_licence``
returns ``None`` so the resolver falls through to community on every
startup, which is the safe default.
"""
from __future__ import annotations

from pathlib import Path

from saebooks.services.licence.caps import caps_for
from saebooks.services.licence.models import LicenceSource, ResolvedLicence


# Public key (Ed25519, raw 32 bytes, base64-URL encoded) that every
# released binary uses to verify USB licences. The private half lives
# on an offline signing host at SAE Engineering; see
# ``saebooks-portal.licensing.usb_signer`` once Wave 5 scaffolds it.
#
# Empty during development. A release build sets this via the
# ``SAEBOOKS_USB_PUBKEY`` env / build variable; an empty value tells
# ``load_usb_licence`` to skip USB entirely.
USB_PUBKEY_B64: str = ""

# Default mount prefixes we scan for a ``saebooks.licence`` file.
# Linux udev mounts removable media under these paths; the Offline
# Windows build is separate concern.
DEFAULT_SCAN_PATHS: tuple[str, ...] = (
    "/run/media",
    "/media",
    "/mnt",
)

LICENCE_FILENAME = "saebooks.licence"


def load_usb_licence(
    scan_paths: tuple[str, ...] = DEFAULT_SCAN_PATHS,
) -> ResolvedLicence | None:
    """Scan ``scan_paths`` for a ``saebooks.licence`` file, verify, return.

    Returns ``None`` in any of the following cases:

    * No ``saebooks.licence`` file found on any mounted drive.
    * The build has no embedded public key (development build).
    * The signature fails verification.
    * The ``usb_uuid`` inside the payload doesn't match the drive's
      hardware serial.
    * The payload parses but names an unknown edition.

    A ``None`` return always means "fall through to the next driver"
    — never raises on a bad licence, because a tampered USB on a
    customer's desk shouldn't crash the app at boot; it should
    silently drop the install back to community and leave a trail in
    the audit log (TODO: emit audit event — wired when the portal is
    live in Wave 5).
    """
    if not USB_PUBKEY_B64:
        return None

    path = _find_licence_file(scan_paths)
    if path is None:
        return None

    # Real verification lands in Wave 5 alongside the portal's signer
    # code. Until then, flagging the path so tests can assert "we
    # scanned and found a file but didn't honour it in dev builds".
    return None


def _find_licence_file(scan_paths: tuple[str, ...]) -> Path | None:
    """Return the path to a ``saebooks.licence`` file, or ``None``."""
    for prefix in scan_paths:
        root = Path(prefix)
        if not root.is_dir():
            continue
        for candidate in root.rglob(LICENCE_FILENAME):
            if candidate.is_file():
                return candidate
    return None


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
    """Build a ``ResolvedLicence`` with ``source=USB`` for tests.

    Bypasses every crypto check. Only call from tests. The resolver
    uses this via monkey-patching in the Wave 3 test suite — Wave 5
    replaces the body of ``load_usb_licence`` and removes the need for
    this helper at runtime.
    """
    return ResolvedLicence(
        edition=edition,
        source=LicenceSource.USB,
        caps=caps_for(edition),
        usb_uuid=usb_uuid,
        licence_id=licence_id,
        licensed_to=licensed_to,
    )
