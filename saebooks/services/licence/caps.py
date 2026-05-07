"""Per-edition seat and company caps (CHARTER v1.1 §12.2 / §12.3).

This is the single source of truth for seat and company caps in the
codebase. Routers that enforce these caps (user creation, company
creation, seat-class change) import from here. The `/admin/license`
matrix renderer reads the same table so the UI and the enforcement
hook can never drift apart.

Representation
--------------

* ``None`` means **unlimited** (Enterprise). Treat every ``cap or 0``
  check with care — use the helpers in ``enforcement.py``.
* ``seat_cap_kind`` is ``"hard"`` or ``"soft"`` per §12.2. Soft caps
  show a warning banner but do not block user creation; hard caps
  block at the limit with an upgrade CTA.

The caps map together with ``_TIER_FLAGS`` in ``services.features``
define everything about an edition — nothing else in the codebase
should hardcode "is this offline" or "how many seats does Business
get".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SeatCapKind = Literal["hard", "soft"]


@dataclass(frozen=True, slots=True)
class EditionCaps:
    """Caps contract for a single edition.

    ``None`` means unlimited (only Enterprise uses this).
    """

    admin_seats: int | None
    employee_seats: int | None
    companies: int | None
    seat_cap_kind: SeatCapKind  # "hard" everywhere except Offline ("soft")

    @property
    def admin_seats_unlimited(self) -> bool:
        return self.admin_seats is None

    @property
    def employee_seats_unlimited(self) -> bool:
        return self.employee_seats is None

    @property
    def companies_unlimited(self) -> bool:
        return self.companies is None


# Source of truth. Mirror CHARTER §12.2 + §12.3 exactly; tests enforce
# the match so a charter amendment that misses this file is caught.
TIER_CAPS: dict[str, EditionCaps] = {
    "community": EditionCaps(
        admin_seats=1, employee_seats=0, companies=1, seat_cap_kind="hard"
    ),
    "offline": EditionCaps(
        admin_seats=1, employee_seats=0, companies=1, seat_cap_kind="soft"
    ),
    "business": EditionCaps(
        admin_seats=2, employee_seats=3, companies=2, seat_cap_kind="hard"
    ),
    "pro": EditionCaps(
        admin_seats=5, employee_seats=10, companies=3, seat_cap_kind="hard"
    ),
    "enterprise": EditionCaps(
        admin_seats=None,
        employee_seats=None,
        companies=None,
        seat_cap_kind="hard",
    ),
}


def caps_for(edition: str) -> EditionCaps:
    """Return the cap contract for ``edition``. Raises on unknown."""
    try:
        return TIER_CAPS[edition]
    except KeyError as exc:
        raise ValueError(f"Unknown edition: {edition!r}") from exc
