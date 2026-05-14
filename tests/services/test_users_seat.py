"""Tests for ``saebooks.services.users`` seat helpers."""
from __future__ import annotations

import uuid

from sqlalchemy import delete

from saebooks.db import AsyncSessionLocal
from saebooks.models.user import User, UserRole
from saebooks.services import users as users_svc
import pytest
pytestmark = pytest.mark.postgres_only


def test_seat_class_for_admin_is_admin() -> None:
    assert users_svc.seat_class_for(UserRole.ADMIN.value) == "admin"


def test_seat_class_for_non_admin_is_employee() -> None:
    for role in (
        UserRole.ACCOUNTANT.value,
        UserRole.BOOKKEEPER.value,
        UserRole.READONLY.value,
        UserRole.CLIENT.value,
    ):
        assert users_svc.seat_class_for(role) == "employee"


def test_seat_class_for_unknown_role_is_employee() -> None:
    """Unknown roles stay out of the admin bucket — fail closed."""
    assert users_svc.seat_class_for("future_role_name") == "employee"


async def test_count_seats_excludes_archived_and_tracks_promotion() -> None:
    tag = uuid.uuid4().hex[:8]
    try:
        async with AsyncSessionLocal() as session:
            admins_before = await users_svc.count_admin_seats(session)
            employees_before = await users_svc.count_employee_seats(session)

            emp = User(
                username=f"SEAT_emp_{tag}",
                role=UserRole.READONLY.value,
            )
            adm = User(
                username=f"SEAT_adm_{tag}",
                role=UserRole.ADMIN.value,
            )
            archived_adm = User(
                username=f"SEAT_arch_{tag}",
                role=UserRole.ADMIN.value,
                archived_at=__import__("datetime").datetime.now(),
            )
            session.add_all([emp, adm, archived_adm])
            await session.commit()

            assert await users_svc.count_admin_seats(session) == admins_before + 1
            assert (
                await users_svc.count_employee_seats(session)
                == employees_before + 1
            )

            # Promote: employee decreases, admin increases.
            emp.role = UserRole.ADMIN.value
            await session.commit()
            assert await users_svc.count_admin_seats(session) == admins_before + 2
            assert (
                await users_svc.count_employee_seats(session) == employees_before
            )
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(User).where(User.username.like(f"SEAT_%_{tag}"))
            )
            await session.commit()
