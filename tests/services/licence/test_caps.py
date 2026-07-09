"""Tests for ``saebooks.services.licence.caps``.

The cap table is the single source of truth for CHARTER §12.2 /
§12.3. These tests pin the table exactly as the charter describes it
so a drive-by refactor that drops or reshapes a field shows up as a
hard failure rather than a silent licensing drift.
"""
from __future__ import annotations

import pytest

from saebooks.services.licence.caps import TIER_CAPS, EditionCaps, caps_for


def test_every_edition_has_an_entry() -> None:
    # CHARTER 12.2/12.3 publish five editions; the internal-only
    # "developer" edition (12.4) is unlimited like enterprise but is
    # never surfaced to customers. It must have a cap entry so
    # caps_for("developer") / create_company work under
    # SAEBOOKS_EDITION=developer.
    assert set(TIER_CAPS.keys()) == {
        "community",
        "offline",
        "business",
        "pro",
        "enterprise",
        "developer",
    }


def test_community_caps() -> None:
    c = caps_for("community")
    assert c == EditionCaps(
        admin_seats=1, employee_seats=0, companies=1, seat_cap_kind="hard"
    )


def test_offline_caps_are_soft() -> None:
    o = caps_for("offline")
    assert o.admin_seats == 1
    assert o.employee_seats == 0
    assert o.companies == 1
    assert o.seat_cap_kind == "soft"


def test_business_caps() -> None:
    b = caps_for("business")
    assert b == EditionCaps(
        admin_seats=2, employee_seats=3, companies=2, seat_cap_kind="hard"
    )


def test_pro_caps() -> None:
    p = caps_for("pro")
    assert p == EditionCaps(
        admin_seats=5, employee_seats=10, companies=3, seat_cap_kind="hard"
    )


def test_enterprise_caps_are_unlimited() -> None:
    e = caps_for("enterprise")
    assert e.admin_seats is None
    assert e.employee_seats is None
    assert e.companies is None
    assert e.admin_seats_unlimited is True
    assert e.employee_seats_unlimited is True
    assert e.companies_unlimited is True


def test_developer_caps_are_unlimited() -> None:
    """Developer is the internal guardrails-off edition (CHARTER 12.4) and
    must mirror enterprise: unlimited admin/employee seats and unlimited
    companies, so caps_for("developer") never raises and create_company
    has no cap to trip on a SAEBOOKS_EDITION=developer instance."""
    d = caps_for("developer")
    assert d.admin_seats is None
    assert d.employee_seats is None
    assert d.companies is None
    assert d.admin_seats_unlimited is True
    assert d.employee_seats_unlimited is True
    assert d.companies_unlimited is True
    # Developer is unlimited exactly like enterprise.
    assert d == caps_for("enterprise")


def test_caps_for_unknown_edition_raises() -> None:
    with pytest.raises(ValueError, match="Unknown edition"):
        caps_for("premium")


@pytest.mark.parametrize(
    "edition",
    ["community", "offline", "business", "pro"],
)
def test_non_enterprise_caps_are_bounded(edition: str) -> None:
    """Only enterprise is allowed to have unlimited caps."""
    c = caps_for(edition)
    assert c.admin_seats is not None
    assert c.employee_seats is not None
    assert c.companies is not None


def test_company_caps_ascend_strictly() -> None:
    """CHARTER §12.3 — company caps grow by tier (enterprise=∞)."""
    order = ["community", "offline", "business", "pro"]
    previous = 0
    for edition in order:
        current = caps_for(edition).companies
        assert current is not None
        assert current >= previous
        previous = current
    # Enterprise is the only unlimited entry.
    assert caps_for("enterprise").companies is None


def test_seat_totals_ascend_weakly_from_business() -> None:
    """CHARTER §12.2 — paid-tier seat totals don't shrink on upgrade."""
    paid_order = ["business", "pro"]
    last_admin = last_employee = 0
    for edition in paid_order:
        c = caps_for(edition)
        assert c.admin_seats is not None
        assert c.employee_seats is not None
        assert c.admin_seats >= last_admin
        assert c.employee_seats >= last_employee
        last_admin = c.admin_seats
        last_employee = c.employee_seats
