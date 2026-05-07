"""Seat- and company-cap enforcement predicates.

Pure functions. Routers call these at creation / role-change time
and act on the result (302 to an upgrade CTA, show a warning banner,
or proceed).

The predicates take the *current count* (fetched from the DB by the
caller) and return a ``CapCheck`` result saying whether the operation
should be blocked, warned-on, or allowed. Emitting an upgrade CTA is
the caller's job — this module doesn't know about HTTP.

This split means:

* Tests can exercise every edge case with simple int inputs, without
  a DB.
* The same logic works from a router (hard-block at 403/402) or from
  a management command (log + exit).
* The soft / hard distinction (CHARTER §12.2) is encoded once, here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from saebooks.services.licence.caps import EditionCaps, caps_for

CheckOutcome = Literal["allow", "warn", "block"]


@dataclass(frozen=True, slots=True)
class CapCheck:
    """Result of a cap check.

    ``outcome``:

    * ``"allow"``  — proceed silently.
    * ``"warn"``   — proceed but show a banner / CTA. Only Offline's
                     soft admin cap ever returns this.
    * ``"block"``  — reject with an upgrade CTA.
    """

    outcome: CheckOutcome
    limit: int | None
    current: int
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.outcome == "block"

    @property
    def should_warn(self) -> bool:
        return self.outcome == "warn"


def _check_seat(
    *,
    current: int,
    limit: int | None,
    kind: str,  # "hard" | "soft"
    class_label: str,  # "admin seat" | "employee seat"
) -> CapCheck:
    """Shared seat-cap logic for admin / employee / company checks."""
    if limit is None:
        return CapCheck(outcome="allow", limit=None, current=current)
    if current < limit:
        return CapCheck(outcome="allow", limit=limit, current=current)
    if kind == "soft":
        return CapCheck(
            outcome="warn",
            limit=limit,
            current=current,
            reason=(
                f"Over the {class_label} limit for this edition "
                f"({current} / {limit}). Functionality retained — soft cap."
            ),
        )
    return CapCheck(
        outcome="block",
        limit=limit,
        current=current,
        reason=(
            f"Edition limit reached: {current} of {limit} {class_label}s in use. "
            f"Upgrade or buy an extra seat to add another."
        ),
    )


def check_admin_seat(edition: str, current_admins: int) -> CapCheck:
    """Can we add one more admin at ``current_admins`` already in place?"""
    caps = caps_for(edition)
    return _check_seat(
        current=current_admins,
        limit=caps.admin_seats,
        kind=caps.seat_cap_kind,
        class_label="admin seat",
    )


def check_employee_seat(edition: str, current_employees: int) -> CapCheck:
    """Can we add one more employee at ``current_employees`` already in place?"""
    caps = caps_for(edition)
    return _check_seat(
        current=current_employees,
        limit=caps.employee_seats,
        kind=caps.seat_cap_kind,
        class_label="employee seat",
    )


def check_company(edition: str, current_companies: int) -> CapCheck:
    """Can we add one more company at ``current_companies`` already in place?

    Company caps are always hard — §12.3. Offline's "soft cap" only
    covers admin seats, not companies.
    """
    caps = caps_for(edition)
    return _check_seat(
        current=current_companies,
        limit=caps.companies,
        kind="hard",
        class_label="company",
    )


def has_capacity_for_role_change(
    *,
    edition: str,
    current_admins: int,
    current_employees: int,
    from_role: str,  # "admin" | "employee"
    to_role: str,  # "admin" | "employee"
) -> CapCheck:
    """Can user A move from ``from_role`` to ``to_role`` right now?

    Role changes move a seat between the admin and employee buckets
    at the same time. Promoting an employee consumes an admin seat
    (+1 admin, -1 employee); demoting does the reverse.

    Only exercises the *destination* cap, because the source bucket
    can only shrink. The caller is expected to have already verified
    that ``from_role`` matches the user's current role.
    """
    if from_role == to_role:
        return CapCheck(outcome="allow", limit=None, current=0)
    caps: EditionCaps = caps_for(edition)
    if to_role == "admin":
        # Promotion: destination is the admin bucket, which grows by 1.
        return _check_seat(
            current=current_admins,
            limit=caps.admin_seats,
            kind=caps.seat_cap_kind,
            class_label="admin seat",
        )
    if to_role == "employee":
        return _check_seat(
            current=current_employees,
            limit=caps.employee_seats,
            kind=caps.seat_cap_kind,
            class_label="employee seat",
        )
    raise ValueError(f"Unknown role: {to_role!r} (expected 'admin' or 'employee')")
