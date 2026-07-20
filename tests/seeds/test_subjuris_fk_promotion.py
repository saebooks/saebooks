"""M1.5 · 5-SUBJURIS (K5 breadth) — sub-jurisdiction FK promotion:
AU state/territory nodes in the T3 tree + the four promoted
``sub_jurisdiction_code`` FK columns (``holiday_calendars``,
``bank_routing_directory``, ``payroll_tax_rates``,
``duty_rate_schedules``).

Three test groups, mirroring ``test_duty_domain_seed.py``:
  * Pure-unit YAML checks (no DB) — ``AU/jurisdictions.yaml`` parses,
    carries exactly the eight ISO 3166-2:AU state/territory nodes, each
    parented on AUS at level ``state`` with ``iso_subdivision_code``
    equal to its primary key. Runs in the standard suite.
  * Pure-unit model checks (no DB) — the four promoted models each carry
    the nullable ``sub_jurisdiction_code`` column FK'd at
    ``jurisdictions.code``, and the legacy ``state`` string column is
    still present (additive transition).
  * Reference-DB integration (gated on REFERENCE_MIGRATION_DATABASE_URL,
    same gate as the loader test) — the seed loads and the eight nodes
    resolve through the tree; a valid ``sub_jurisdiction_code`` insert
    passes and a bogus one is REJECTED by the FK; the migration-0016
    backfill resolution ('QLD' → 'AU-QLD' via parent + ISO suffix) fills
    both the jurisdiction-carrying and the AU-implicit
    (``bank_routing_directory``) shapes; a national (NULL-state) holiday
    row stays NULL.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference import (
    BankRoutingDirectoryEntry,
    DutyRateSchedule,
    HolidayCalendar,
    Jurisdiction,
    PayrollTaxRate,
)

_AU_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
    / "jurisdictions.yaml"
)

_EXPECTED_CODES = {
    "AU-NSW",
    "AU-VIC",
    "AU-QLD",
    "AU-SA",
    "AU-WA",
    "AU-TAS",
    "AU-NT",
    "AU-ACT",
}

_PROMOTED_MODELS = (
    HolidayCalendar,
    BankRoutingDirectoryEntry,
    PayrollTaxRate,
    DutyRateSchedule,
)

# The migration-0016 backfill statements, exercised verbatim against the
# live seeded tree so the resolution rule ('QLD' matches the node whose
# parent is the row's country and whose ISO 3166-2 suffix is the state
# string) is proven against real data, not just by reading the migration.
_BACKFILL_WITH_JURISDICTION = """
    UPDATE {table} AS t
    SET sub_jurisdiction_code = j.code
    FROM jurisdictions AS j
    WHERE t.sub_jurisdiction_code IS NULL
      AND t.state IS NOT NULL
      AND j.parent_code = t.jurisdiction
      AND split_part(j.iso_subdivision_code, '-', 2) = t.state
"""

_BACKFILL_AU_IMPLICIT = """
    UPDATE {table} AS t
    SET sub_jurisdiction_code = j.code
    FROM jurisdictions AS j
    WHERE t.sub_jurisdiction_code IS NULL
      AND t.state IS NOT NULL
      AND j.parent_code = 'AUS'
      AND split_part(j.iso_subdivision_code, '-', 2) = t.state
"""


def test_au_subdivision_seed_valid() -> None:
    doc = yaml.safe_load(_AU_SEED.read_text())
    assert doc["table"] == "jurisdictions"
    assert doc["key"] == ["code"]
    rows = doc["rows"]
    assert rows, "AU sub-jurisdiction seed is empty"

    codes = [r["code"] for r in rows]
    assert len(codes) == len(set(codes)), "duplicate sub-jurisdiction codes"
    assert set(codes) == _EXPECTED_CODES, (
        f"expected the eight ISO 3166-2:AU nodes, got {sorted(codes)}"
    )

    for r in rows:
        assert r["parent_code"] == "AUS", f"{r['code']} must parent on AUS"
        assert r["level"] == "state", f"{r['code']} must be state-level"
        assert r["iso_subdivision_code"] == r["code"], (
            f"{r['code']}: sub-national PK is its ISO 3166-2 code"
        )
        assert r["currency_default"] == "AUD"
        assert len(r["code"]) <= 6, (
            f"{r['code']} exceeds jurisdictions.code String(6)"
        )
        # The FK-promotion backfill maps legacy state strings on the ISO
        # suffix — every node must expose one ('AU-QLD' → 'QLD').
        suffix = r["code"].split("-", 1)[1]
        assert 2 <= len(suffix) <= 3


def test_promoted_models_carry_fk_column() -> None:
    assert Jurisdiction.__table__.c.code.type.length == 6
    assert Jurisdiction.__table__.c.parent_code.type.length == 6
    for model in _PROMOTED_MODELS:
        cols = model.__table__.c
        assert "state" in cols, (
            f"{model.__name__}: legacy state column must survive the "
            "additive transition"
        )
        col = cols["sub_jurisdiction_code"]
        assert col.nullable, f"{model.__name__}.sub_jurisdiction_code must be nullable"
        fk_targets = {fk.target_fullname for fk in col.foreign_keys}
        assert fk_targets == {"jurisdictions.code"}, (
            f"{model.__name__}.sub_jurisdiction_code must FK jurisdictions.code, "
            f"got {fk_targets}"
        )


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_au_subdivisions_load_and_resolve() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-5subjuris")
    assert "AU/jurisdictions.yaml" in counts, (
        f"loader skipped AU/jurisdictions.yaml: {sorted(counts)}"
    )
    assert counts["AU/jurisdictions.yaml"] == 8

    async with ReferenceMigrationSession() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT code, parent_code, level, iso_subdivision_code "
                    "FROM jurisdictions WHERE parent_code = 'AUS' "
                    "ORDER BY code"
                )
            )
        ).all()
        assert {r.code for r in rows} == _EXPECTED_CODES
        for r in rows:
            assert r.level == "state"
            assert r.iso_subdivision_code == r.code

        # The tree resolves child → country, same join every consumer uses.
        parent_name = (
            await s.execute(
                text(
                    "SELECT p.name FROM jurisdictions c "
                    "JOIN jurisdictions p ON p.code = c.parent_code "
                    "WHERE c.code = 'AU-QLD'"
                )
            )
        ).scalar_one()
        assert parent_name == "Australia"


@pytestmark_ref
@pytest.mark.asyncio
async def test_fk_accepts_seeded_node_and_rejects_bogus() -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, IntegrityError

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    await load_seeds("AU", version_tag="test-5subjuris")

    ins = (
        "INSERT INTO duty_rate_schedules "
        "(id, jurisdiction, state, sub_jurisdiction_code, transaction_type, "
        " lower_bound, upper_bound, rate, base_amount) "
        "VALUES (gen_random_uuid(), 'AUS', 'QLD', :code, "
        " 'motor_vehicle', 0, NULL, 2.0, 0)"
    )
    try:
        async with ReferenceMigrationSession() as s:
            await s.execute(text(ins), {"code": "AU-QLD"})
            await s.commit()

        async with ReferenceMigrationSession() as s:
            with pytest.raises((IntegrityError, DBAPIError)):
                await s.execute(text(ins), {"code": "AU-XXX"})
                await s.commit()
    finally:
        async with ReferenceMigrationSession() as s:
            await s.execute(
                text(
                    "DELETE FROM duty_rate_schedules WHERE jurisdiction = 'AUS' "
                    "AND state = 'QLD' AND transaction_type = 'motor_vehicle'"
                )
            )
            await s.commit()


@pytestmark_ref
@pytest.mark.asyncio
async def test_backfill_resolution_fills_au_rows() -> None:
    """The migration-0016 backfill rule, proven against the live tree:
    a jurisdiction-carrying row ('AUS'/'QLD') and an AU-implicit
    ``bank_routing_directory`` row ('QLD') both resolve to 'AU-QLD';
    a national (NULL-state) holiday stays NULL."""
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    await load_seeds("AU", version_tag="test-5subjuris")

    marker = "5SUBJURIS-BACKFILL-TEST"
    try:
        async with ReferenceMigrationSession() as s:
            await s.execute(
                text(
                    "INSERT INTO payroll_tax_rates "
                    "(id, jurisdiction, state, fy_year, threshold, rate) "
                    "VALUES (gen_random_uuid(), 'AUS', 'QLD', 1901, "
                    " 1300000, 4.75)"
                )
            )
            await s.execute(
                text(
                    "INSERT INTO bank_routing_directory "
                    "(id, bsb, bank_name, state) "
                    "VALUES (gen_random_uuid(), '999901', :marker, 'QLD')"
                ),
                {"marker": marker},
            )
            await s.execute(
                text(
                    "INSERT INTO holiday_calendars "
                    "(id, jurisdiction, state, holiday_date, name, "
                    " is_business_day_substituted) "
                    "VALUES (gen_random_uuid(), 'AUS', NULL, '1901-01-01', "
                    " :marker, false)"
                ),
                {"marker": marker},
            )
            await s.commit()

        async with ReferenceMigrationSession() as s:
            for table, tmpl in (
                ("payroll_tax_rates", _BACKFILL_WITH_JURISDICTION),
                ("bank_routing_directory", _BACKFILL_AU_IMPLICIT),
                ("holiday_calendars", _BACKFILL_WITH_JURISDICTION),
            ):
                await s.execute(text(tmpl.format(table=table)))
            await s.commit()

        async with ReferenceMigrationSession() as s:
            payroll_code = (
                await s.execute(
                    text(
                        "SELECT sub_jurisdiction_code FROM payroll_tax_rates "
                        "WHERE jurisdiction = 'AUS' AND state = 'QLD' "
                        "AND fy_year = 1901"
                    )
                )
            ).scalar_one()
            assert payroll_code == "AU-QLD"

            routing_code = (
                await s.execute(
                    text(
                        "SELECT sub_jurisdiction_code FROM bank_routing_directory "
                        "WHERE bank_name = :marker"
                    ),
                    {"marker": marker},
                )
            ).scalar_one()
            assert routing_code == "AU-QLD"

            holiday_code = (
                await s.execute(
                    text(
                        "SELECT sub_jurisdiction_code FROM holiday_calendars "
                        "WHERE name = :marker"
                    ),
                    {"marker": marker},
                )
            ).scalar_one()
            assert holiday_code is None, (
                "national (NULL-state) holiday must not be backfilled"
            )
    finally:
        async with ReferenceMigrationSession() as s:
            await s.execute(
                text(
                    "DELETE FROM payroll_tax_rates WHERE jurisdiction = 'AUS' "
                    "AND state = 'QLD' AND fy_year = 1901"
                )
            )
            await s.execute(
                text("DELETE FROM bank_routing_directory WHERE bank_name = :m"),
                {"m": marker},
            )
            await s.execute(
                text("DELETE FROM holiday_calendars WHERE name = :m"),
                {"m": marker},
            )
            await s.commit()
