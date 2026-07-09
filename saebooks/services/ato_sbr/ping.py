"""HTTPS reachability check against the ATO SBR authentication gateway —
COMMUNITY EDITION STUB.

The full implementation is the smoke-test button behind the ATO SBR
onboarding wizard: a live reachability GET against the EVTE/production
gateway base URL. Onboarding a real ATO Machine Credential (and confirming
connectivity to ATO's environments) is part of the commercial SAE Books
e-lodgement feature — see CHARTER.md / LICENSING.md. ``ping_environment``
raises ``NotImplementedError`` in this edition; ``PingResult`` stays defined
so callers keep their import surface.
"""
from __future__ import annotations

from dataclasses import dataclass

from saebooks.config import Settings


@dataclass(frozen=True)
class PingResult:
    ok: bool
    status_code: int | None
    url: str
    detail: str


async def ping_environment(
    environment: str, *, settings: Settings
) -> PingResult:
    """Return a ``PingResult`` describing one GET round-trip.

    COMMUNITY EDITION STUB — always raises. See module docstring.
    """
    raise NotImplementedError(
        "Certified e-lodgement is a commercial SAE Books feature; the community "
        "edition ships box definitions + the return calculator but not the "
        "regulator transmission adapters. See CHARTER.md / LICENSING.md."
    )
