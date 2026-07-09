"""Licence resolution + seat/company cap enforcement.

Public API:

* ``resolve_licence()`` — returns the boot-time ``ResolvedLicence``.
* ``caps_for(edition)`` — ``EditionCaps`` lookup table.
* ``check_admin_seat`` / ``check_employee_seat`` / ``check_company``
  — pure predicates returning ``CapCheck`` for router use.
* ``has_capacity_for_role_change`` — pre-check for seat class swaps.
* ``ResolvedLicence`` / ``EditionCaps`` / ``CapCheck`` /
  ``LicenceSource`` — data shapes.

Wave-3 scope: scaffolded drivers (USB + JWT) return ``None`` in
development builds so the resolver always falls through to community.
Wave-5 lands the real Ed25519 + JWT verification alongside the
portal service.
"""
from saebooks.services.licence.caps import (
    TIER_CAPS,
    EditionCaps,
    SeatCapKind,
    caps_for,
)
from saebooks.services.licence.enforcement import (
    CapCheck,
    CheckOutcome,
    check_admin_seat,
    check_company,
    check_employee_seat,
    has_capacity_for_role_change,
)
from saebooks.services.licence.models import LicenceSource, ResolvedLicence
from saebooks.services.licence.resolver import (
    resolve_licence,
    resolve_licence_for_user,
)

__all__ = [
    "TIER_CAPS",
    "CapCheck",
    "CheckOutcome",
    "EditionCaps",
    "LicenceSource",
    "LicenseService",
    "LicenseSnapshot",
    "ResolvedLicence",
    "SeatCapKind",
    "caps_for",
    "check_admin_seat",
    "check_company",
    "check_employee_seat",
    "has_capacity_for_role_change",
    "resolve_licence",
    "resolve_licence_for_user",
]

from saebooks.services.licence.service import (
    LicenseService,
    LicenseSnapshot,
)
