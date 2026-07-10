"""Portal-JWT licence driver — PUBLIC SHIM (commercial validation stubbed).

The private build verifies portal-issued subscription JWTs (Business / Pro /
Enterprise) against SAE Books' Ed25519 portal key. That key and the validation
logic are the commercial control plane and are NOT shipped in the open repo.

This shim keeps the public symbols (so anything importing the driver still
resolves) but performs NO validation: every entry point returns ``None`` (no
licence), and no portal public key is baked in. The community resolver
(``resolver.py`` shim) does not call these — a self-host selects its edition via
``SAEBOOKS_EDITION`` — but the module is preserved as an explicit seam.
"""
from __future__ import annotations

import logging
from typing import Any

from saebooks.services.licence.caps import caps_for
from saebooks.services.licence.models import LicenceSource, ResolvedLicence

log = logging.getLogger(__name__)

# No portal public key in the community build.
PORTAL_PUBKEY_B64: str = ""
MAX_GRACE_DAYS: int = 60
DEFAULT_JWT_CACHE_PATH: str = "/var/lib/saebooks/licence.jwt"


def _load_portal_public_key() -> None:
    """No portal key in the community build."""
    return None


def _verify_and_decode(token: str, pubkey: Any) -> None:
    """Commercial JWT verification is not available in the community build."""
    return None


def load_portal_jwt(cache_path: str = DEFAULT_JWT_CACHE_PATH) -> None:
    """No portal-JWT resolution in the community build."""
    return None


def build_fake_licence_for_tests(
    *,
    edition: str = "business",
    ledger_id: str = "test-ledger",
    licensed_to: str = "Test Holder",
) -> ResolvedLicence:
    """Build a ``ResolvedLicence`` with ``source=JWT`` for tests."""
    return ResolvedLicence(
        edition=edition,
        source=LicenceSource.JWT,
        caps=caps_for(edition),
        ledger_id=ledger_id,
        licensed_to=licensed_to,
    )
