"""M1.5 P1 tail — ``holiday_calendars.calendar_scope`` discriminator.

Mirrors ``test_subjuris_fk_promotion.py``'s structure: a pure-unit model
check (no DB) plus a reference-DB integration check gated on
``REFERENCE_MIGRATION_DATABASE_URL``.
"""
from __future__ import annotations

import os

import pytest

from saebooks.models.reference import HolidayCalendar


def test_calendar_scope_column_nullable_string() -> None:
    col = HolidayCalendar.__table__.columns["calendar_scope"]
    assert col.nullable
    assert col.type.length == 8


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_calendar_scope_persists_and_null_stays_unscoped() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession

    marker = "HOLCAL-SCOPE-TEST"
    try:
        async with ReferenceMigrationSession() as s:
            await s.execute(
                text(
                    "INSERT INTO holiday_calendars "
                    "(id, jurisdiction, state, holiday_date, name, "
                    " is_business_day_substituted, calendar_scope) "
                    "VALUES (gen_random_uuid(), 'AUS', NULL, '1901-01-02', "
                    " :marker, false, 'filing')"
                ),
                {"marker": marker},
            )
            await s.execute(
                text(
                    "INSERT INTO holiday_calendars "
                    "(id, jurisdiction, state, holiday_date, name, "
                    " is_business_day_substituted) "
                    "VALUES (gen_random_uuid(), 'AUS', NULL, '1901-01-03', "
                    " :marker2, false)"
                ),
                {"marker2": marker + "-unscoped"},
            )
            await s.commit()

        async with ReferenceMigrationSession() as s:
            scoped = (
                await s.execute(
                    text(
                        "SELECT calendar_scope FROM holiday_calendars WHERE name = :marker"
                    ),
                    {"marker": marker},
                )
            ).scalar_one()
            unscoped = (
                await s.execute(
                    text(
                        "SELECT calendar_scope FROM holiday_calendars WHERE name = :marker2"
                    ),
                    {"marker2": marker + "-unscoped"},
                )
            ).scalar_one()
            assert scoped == "filing"
            assert unscoped is None
    finally:
        async with ReferenceMigrationSession() as s:
            await s.execute(
                text("DELETE FROM holiday_calendars WHERE name LIKE :pfx"),
                {"pfx": f"{marker}%"},
            )
            await s.commit()
