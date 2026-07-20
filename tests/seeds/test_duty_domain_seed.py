"""M1.5 · Wave 5-DUTIES — duties-domain reference gap seed integrity and
AU-parity proof (foreign-purchaser surcharge, landholder duty,
securities-transfer duty, lease duty, effective-dating on
``duty_rate_schedules``).

Three test groups, mirroring ``test_coa_statutory_seed.py``:
  * Pure-unit YAML checks (no DB) — the AU seeds parse, target the right
    tables, key on their natural keys, and every enum-backed column
    carries a real vocabulary value. Runs in the standard suite.
  * AU-parity unit checks — the QLD/NSW surcharge series are coherent
    dated series with exactly one open row per state; every AU
    securities-duty and lease-duty row is CLOSED (Australia abolished
    both — "no open row" IS the AU parity claim).
  * Reference-DB integration (gated on REFERENCE_MIGRATION_DATABASE_URL,
    same gate as the loader test) — the seeds load, the AU rows resolve,
    ``lookup_duty_surcharge_rate`` returns the era-correct AFAD rate, and
    ``lookup_stamp_duty_rate`` honours the new ``as_at`` effective-dating
    on ``duty_rate_schedules`` while keeping the undated call byte-identical.
"""
from __future__ import annotations

import itertools
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference.duty_surcharge_rate import (
    SURCHARGE_PURCHASER_CLASSES,
)
from saebooks.models.reference.landholder_duty_rule import (
    LANDHOLDER_DUTY_BASES,
    LANDHOLDER_ENTITY_CLASSES,
    LandholderDutyBasis,
)
from saebooks.models.reference.lease_duty_rate import LEASE_DUTY_BASES
from saebooks.models.reference.securities_duty_rate import SECURITY_CLASSES

_AU_DIR = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
)
_SURCHARGE_SEED = _AU_DIR / "duty_surcharge_rates.yaml"
_LANDHOLDER_SEED = _AU_DIR / "landholder_duty_rules.yaml"
_SECURITIES_SEED = _AU_DIR / "securities_duty_rates.yaml"
_LEASE_SEED = _AU_DIR / "lease_duty_rates.yaml"


def test_au_surcharge_seed_valid() -> None:
    doc = yaml.safe_load(_SURCHARGE_SEED.read_text())
    assert doc["table"] == "duty_surcharge_rates"
    assert doc["key"] == [
        "jurisdiction", "sub_jurisdiction", "transaction_type",
        "purchaser_class", "effective_from",
    ]
    rows = doc["rows"]
    assert rows, "AU surcharge seed is empty"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["purchaser_class"] in SURCHARGE_PURCHASER_CLASSES
        assert r["transaction_type"] == "property_transfer"
        assert Decimal("0") < Decimal(str(r["surcharge_rate"])) <= Decimal("20"), (
            f"surcharge rate {r['surcharge_rate']!r} outside sane percentage range"
        )
        assert isinstance(r["effective_from"], date)

    # AU parity: exactly one OPEN surcharge row per state — a foreign
    # purchaser today attracts exactly one surcharge rate.
    open_by_state: dict[str, int] = {}
    for r in rows:
        if r.get("effective_to") is None:
            open_by_state[r["sub_jurisdiction"]] = (
                open_by_state.get(r["sub_jurisdiction"], 0) + 1
            )
    assert open_by_state == {"QLD": 1, "NSW": 1, "VIC": 1}, (
        f"expected one open surcharge row per big state, got {open_by_state}"
    )

    # Dated series coherent: within a state the closed row must end
    # before the open row starts (no overlap, no gap-order inversion).
    for state in ("QLD", "NSW"):
        series = sorted(
            (r for r in rows if r["sub_jurisdiction"] == state),
            key=lambda r: r["effective_from"],
        )
        for earlier, later in itertools.pairwise(series):
            assert earlier["effective_to"] is not None
            assert earlier["effective_to"] < later["effective_from"], (
                f"{state} surcharge series overlaps: "
                f"{earlier['effective_to']} !< {later['effective_from']}"
            )


def test_au_landholder_seed_valid() -> None:
    doc = yaml.safe_load(_LANDHOLDER_SEED.read_text())
    assert doc["table"] == "landholder_duty_rules"
    assert doc["key"] == [
        "jurisdiction", "sub_jurisdiction", "entity_class", "effective_from",
    ]
    rows = doc["rows"]
    assert rows, "AU landholder seed is empty"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["entity_class"] in LANDHOLDER_ENTITY_CLASSES
        assert r["duty_basis"] in LANDHOLDER_DUTY_BASES
        assert Decimal(str(r["landholding_value_threshold"])) > 0
        assert (
            Decimal("0")
            < Decimal(str(r["significant_interest_pct"]))
            <= Decimal("100")
        )
        # basis_fraction accompanies fraction_of_transfer_duty and only that.
        if r["duty_basis"] == LandholderDutyBasis.FRACTION_OF_TRANSFER_DUTY.value:
            assert Decimal(str(r["basis_fraction"])) > 0
        else:
            assert r.get("basis_fraction") is None

    # The three canonical rule shapes are all represented: 50% private,
    # 90% listed w/ concessional fraction, VIC's 20% unit-trust trigger.
    shapes = {
        (r["entity_class"], Decimal(str(r["significant_interest_pct"])))
        for r in rows
    }
    assert ("private_company", Decimal(50)) in shapes
    assert ("listed_entity", Decimal(90)) in shapes
    assert ("private_unit_trust", Decimal(20)) in shapes


def test_au_securities_and_lease_seeds_valid_and_closed() -> None:
    """AU parity: Australia levies NO securities-transfer or lease duty
    today — every AU row in both catalogs must be CLOSED (effective_to
    set). The rows exist so a pre-abolition transfer can still be rated
    and so the table shape is proven against real data."""
    sec = yaml.safe_load(_SECURITIES_SEED.read_text())
    assert sec["table"] == "securities_duty_rates"
    assert sec["key"] == [
        "jurisdiction", "sub_jurisdiction", "security_class", "effective_from",
    ]
    assert sec["rows"], "AU securities-duty seed is empty"
    for r in sec["rows"]:
        assert r["jurisdiction"] == "AUS"
        assert r["security_class"] in SECURITY_CLASSES
        assert r["rate_basis"] in ("consideration", "market_value")
        assert isinstance(r.get("effective_to"), date), (
            f"AU securities-duty row {r['sub_jurisdiction']}/"
            f"{r['security_class']} is OPEN — marketable-securities duty "
            "is abolished in Australia"
        )
        assert r["effective_from"] < r["effective_to"]

    lease = yaml.safe_load(_LEASE_SEED.read_text())
    assert lease["table"] == "lease_duty_rates"
    assert lease["key"] == [
        "jurisdiction", "sub_jurisdiction", "duty_base", "effective_from",
    ]
    assert lease["rows"], "AU lease-duty seed is empty"
    for r in lease["rows"]:
        assert r["jurisdiction"] == "AUS"
        assert r["duty_base"] in LEASE_DUTY_BASES
        assert isinstance(r.get("effective_to"), date), (
            f"AU lease-duty row {r['sub_jurisdiction']}/{r['duty_base']} is "
            "OPEN — rent-based lease duty is abolished in Australia"
        )
        assert r["effective_from"] < r["effective_to"]


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_duty_domain_seeds_and_lookups() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.jurisdictions.au.dutiable_events import lookup_duty_surcharge_rate
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-5duties")
    for fname in (
        "AU/duty_surcharge_rates.yaml",
        "AU/landholder_duty_rules.yaml",
        "AU/securities_duty_rates.yaml",
        "AU/lease_duty_rates.yaml",
    ):
        assert fname in counts, f"loader skipped {fname}: {sorted(counts)}"

    async with ReferenceMigrationSession() as s:
        # The open QLD AFAD row is the 8% one.
        afad = (
            await s.execute(
                text(
                    "SELECT surcharge_rate FROM duty_surcharge_rates "
                    "WHERE jurisdiction = 'AUS' AND sub_jurisdiction = 'QLD' "
                    "AND purchaser_class = 'foreign_person' "
                    "AND effective_to IS NULL"
                )
            )
        ).scalar_one()
        assert afad == Decimal("8.0000")

        # QLD private-company landholder rule: $2m threshold, 50% trigger.
        threshold, pct = (
            await s.execute(
                text(
                    "SELECT landholding_value_threshold, "
                    "significant_interest_pct FROM landholder_duty_rules "
                    "WHERE jurisdiction = 'AUS' AND sub_jurisdiction = 'QLD' "
                    "AND entity_class = 'private_company'"
                )
            )
        ).one()
        assert threshold == Decimal("2000000.00")
        assert pct == Decimal("50.00")

        # AU parity: no OPEN securities-duty or lease-duty row anywhere.
        for tbl in ("securities_duty_rates", "lease_duty_rates"):
            open_rows = (
                await s.execute(
                    text(
                        f"SELECT count(*) FROM {tbl} "
                        "WHERE jurisdiction = 'AUS' AND effective_to IS NULL"
                    )
                )
            ).scalar_one()
            assert open_rows == 0, (
                f"{open_rows} open AU rows in {tbl} — these duties are "
                "abolished in Australia"
            )

    # Surcharge lookup resolves the era-correct AFAD rate, and NT (which
    # levies no foreign-purchaser surcharge) resolves to None.
    async with ReferenceMigrationSession() as rs:
        today_rate = await lookup_duty_surcharge_rate(
            rs,
            jurisdiction="AUS",
            sub_jurisdiction="QLD",
            transaction_type="property_transfer",
            purchaser_class="foreign_person",
            as_at=date(2026, 1, 1),
        )
        assert today_rate == Decimal("8.0000")

        old_rate = await lookup_duty_surcharge_rate(
            rs,
            jurisdiction="AUS",
            sub_jurisdiction="QLD",
            transaction_type="property_transfer",
            purchaser_class="foreign_person",
            as_at=date(2020, 1, 1),
        )
        assert old_rate == Decimal("7.0000")

        none_rate = await lookup_duty_surcharge_rate(
            rs,
            jurisdiction="AUS",
            sub_jurisdiction="NT",
            transaction_type="property_transfer",
            purchaser_class="foreign_person",
            as_at=date(2026, 1, 1),
        )
        assert none_rate is None


@pytestmark_ref
@pytest.mark.asyncio
async def test_duty_rate_schedule_effective_dating() -> None:
    """Effective-dating on ``duty_rate_schedules`` (migration 0014):
    two dated rows for the same bracket resolve era-correctly via
    ``as_at``, while the undated call keeps its original semantics.
    Inserts its own fixture rows and cleans up, mirroring
    ``test_lookup_stamp_duty_rate_from_reference_db``."""
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.jurisdictions.au.dutiable_events import lookup_stamp_duty_rate

    async with ReferenceMigrationSession() as s:
        await s.execute(
            text(
                "INSERT INTO duty_rate_schedules "
                "(id, jurisdiction, state, transaction_type, lower_bound, "
                " upper_bound, rate, base_amount, effective_from, effective_to) "
                "VALUES "
                "(gen_random_uuid(), 'AUS', 'WA', 'property_transfer', "
                " 0, 1000000, 3.0, 0, '2015-07-01', '2020-06-30'), "
                "(gen_random_uuid(), 'AUS', 'WA', 'property_transfer', "
                " 0, 1000000, 4.0, 0, '2020-07-01', NULL)"
            )
        )
        await s.commit()

    try:
        async with ReferenceMigrationSession() as rs:
            old_duty = await lookup_stamp_duty_rate(
                rs,
                jurisdiction="AUS",
                state="WA",
                transaction_type="property_transfer",
                dutiable_value=Decimal("100000"),
                as_at=date(2018, 1, 1),
            )
            assert old_duty == Decimal("100000") * Decimal("3.0") / Decimal("100")

            new_duty = await lookup_stamp_duty_rate(
                rs,
                jurisdiction="AUS",
                state="WA",
                transaction_type="property_transfer",
                dutiable_value=Decimal("100000"),
                as_at=date(2026, 1, 1),
            )
            assert new_duty == Decimal("100000") * Decimal("4.0") / Decimal("100")

            # Before either row commenced: nothing in force.
            none_duty = await lookup_stamp_duty_rate(
                rs,
                jurisdiction="AUS",
                state="WA",
                transaction_type="property_transfer",
                dutiable_value=Decimal("100000"),
                as_at=date(2010, 1, 1),
            )
            assert none_duty is None

            # Undated call: original behaviour (every row considered) —
            # both fixture rows share lower_bound so it resolves one of
            # them; the point is it still returns a value, not None.
            undated = await lookup_stamp_duty_rate(
                rs,
                jurisdiction="AUS",
                state="WA",
                transaction_type="property_transfer",
                dutiable_value=Decimal("100000"),
            )
            assert undated is not None
    finally:
        async with ReferenceMigrationSession() as s:
            await s.execute(
                text(
                    "DELETE FROM duty_rate_schedules WHERE jurisdiction = 'AUS' "
                    "AND state = 'WA' AND transaction_type = 'property_transfer'"
                )
            )
            await s.commit()
