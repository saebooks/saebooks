"""Tests for ``saebooks.services.licence.enforcement``."""
from __future__ import annotations

import pytest

from saebooks.services.licence.enforcement import (
    check_admin_seat,
    check_company,
    check_employee_seat,
    has_capacity_for_role_change,
)

# --------------------------------------------------------------------- #
# check_admin_seat                                                      #
# --------------------------------------------------------------------- #


def test_admin_seat_allows_when_below_cap() -> None:
    r = check_admin_seat("business", current_admins=1)
    assert r.outcome == "allow"
    assert r.limit == 2
    assert r.current == 1


def test_admin_seat_blocks_at_hard_cap() -> None:
    r = check_admin_seat("business", current_admins=2)
    assert r.outcome == "block"
    assert r.blocked is True
    assert "2 of 2" in r.reason


def test_admin_seat_warns_at_offline_soft_cap() -> None:
    r = check_admin_seat("offline", current_admins=1)
    assert r.outcome == "warn"
    assert r.should_warn is True
    assert r.blocked is False
    assert "soft cap" in r.reason


def test_admin_seat_allows_unlimited_on_enterprise() -> None:
    r = check_admin_seat("enterprise", current_admins=50_000)
    assert r.outcome == "allow"
    assert r.limit is None


# --------------------------------------------------------------------- #
# check_employee_seat                                                   #
# --------------------------------------------------------------------- #


def test_employee_seat_blocks_on_community() -> None:
    # Community has 0 employee seats — first attempt is a block.
    r = check_employee_seat("community", current_employees=0)
    assert r.outcome == "block"


def test_employee_seat_allows_up_to_business_cap() -> None:
    r = check_employee_seat("business", current_employees=2)
    assert r.outcome == "allow"


def test_employee_seat_blocks_at_pro_cap() -> None:
    r = check_employee_seat("pro", current_employees=10)
    assert r.outcome == "block"


def test_employee_seat_allows_unlimited_on_enterprise() -> None:
    r = check_employee_seat("enterprise", current_employees=999)
    assert r.outcome == "allow"


# --------------------------------------------------------------------- #
# check_company                                                         #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("edition", "current", "expected"),
    [
        ("community", 0, "allow"),
        ("community", 1, "block"),
        ("offline", 0, "allow"),
        ("offline", 1, "block"),   # §12.3: company cap is always hard
        ("business", 1, "allow"),
        ("business", 2, "block"),
        ("pro", 2, "allow"),
        ("pro", 3, "block"),
        ("enterprise", 10_000, "allow"),
    ],
)
def test_check_company_matrix(edition: str, current: int, expected: str) -> None:
    assert check_company(edition, current).outcome == expected


# --------------------------------------------------------------------- #
# has_capacity_for_role_change                                          #
# --------------------------------------------------------------------- #


def test_role_change_no_op_allowed() -> None:
    r = has_capacity_for_role_change(
        edition="business",
        current_admins=2,
        current_employees=3,
        from_role="admin",
        to_role="admin",
    )
    assert r.outcome == "allow"


def test_role_change_promotion_blocked_when_admin_cap_full() -> None:
    """Business has 2 admins; promoting a 3rd should block."""
    r = has_capacity_for_role_change(
        edition="business",
        current_admins=2,
        current_employees=3,
        from_role="employee",
        to_role="admin",
    )
    assert r.outcome == "block"


def test_role_change_demotion_allowed_when_employee_has_room() -> None:
    """Business has 3 employees; demoting one with 2 employees is fine."""
    r = has_capacity_for_role_change(
        edition="business",
        current_admins=2,
        current_employees=2,
        from_role="admin",
        to_role="employee",
    )
    assert r.outcome == "allow"


def test_role_change_demotion_blocked_when_employee_cap_full() -> None:
    """Business has 3 employees; demoting a 4th would overflow."""
    r = has_capacity_for_role_change(
        edition="business",
        current_admins=2,
        current_employees=3,
        from_role="admin",
        to_role="employee",
    )
    assert r.outcome == "block"


def test_role_change_on_enterprise_always_allowed() -> None:
    r = has_capacity_for_role_change(
        edition="enterprise",
        current_admins=1_000,
        current_employees=10_000,
        from_role="employee",
        to_role="admin",
    )
    assert r.outcome == "allow"


def test_role_change_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="Unknown role"):
        has_capacity_for_role_change(
            edition="business",
            current_admins=0,
            current_employees=0,
            from_role="admin",
            to_role="owner",
        )
