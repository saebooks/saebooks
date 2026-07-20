"""EE-frontier build plan Module 2 — OSS-Q member-state rate seed integrity
+ reference-DB load. Mirrors ``tests/seeds/test_duty_concession_seed.py``'s
shape: a pure-unit seed-shape check (no DB) plus a reference-DB
integration test gated on ``REFERENCE_MIGRATION_DATABASE_URL``.
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from saebooks.services.lodgement.oss_q.mapping import (
    ALPHA2_TO_ALPHA3,
    MEMBER_STATE_NAMES,
)

_SEED = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "EE"
    / "oss_member_state_rates.yaml"
)


def test_oss_member_state_rates_seed_shape() -> None:
    doc = yaml.safe_load(_SEED.read_text())
    assert doc["table"] == "oss_member_state_rates"
    assert doc["key"] == ["country_code", "effective_from"]
    rows = doc["rows"]
    assert rows, "OSS member-state rate seed is empty"

    # No duplicate natural key.
    keys = [(r["country_code"], r["effective_from"]) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (country_code, effective_from) in OSS rate seed"

    # Every seeded country_code is one of the 15 alpha-3 codes this
    # package's mapping.py recognises (kept in lock-step by inspection —
    # see mapping.py's own comment on this discipline), and every rate
    # is a plausible EU VAT percentage (sanity bound, not a precise
    # verification — see the seed file's own UNVERIFIED header).
    known_alpha3 = set(ALPHA2_TO_ALPHA3.values())
    seen_alpha3 = set()
    for r in rows:
        assert r["country_code"] in known_alpha3, (
            f"row country_code {r['country_code']!r} not in mapping.py's "
            f"known alpha-3 set {sorted(known_alpha3)}"
        )
        seen_alpha3.add(r["country_code"])
        rate = Decimal(str(r["standard_vat_rate_percent"]))
        assert Decimal("15") <= rate <= Decimal("27"), (
            f"row {r['country_code']!r} rate {rate} outside plausible EU "
            "standard-VAT-rate bounds (15-27%) — check for a units error"
        )

    # Every alpha-3 mapping.py knows about is actually seeded here (no
    # silent gap between the two, since generator.py's embedded fallback
    # is meant to mirror this file exactly).
    assert seen_alpha3 == known_alpha3, (
        f"seed/mapping mismatch: seeded={sorted(seen_alpha3)} "
        f"known={sorted(known_alpha3)}"
    )

    # Estonia itself must never appear — OSS reports cross-border supply
    # to ANOTHER member state, EST is never its own OSS destination
    # (0011's migration docstring + this seed's own header note).
    assert "EST" not in seen_alpha3


def test_oss_member_state_rates_seed_matches_generator_embedded_fallback() -> None:
    """The seed's rates must match generator.py's embedded fallback
    exactly — the "kept in lock-step by inspection" discipline this
    module's docstrings claim, actually checked."""
    from saebooks.services.lodgement.oss_q.generator import _EMBEDDED_STANDARD_RATES
    from saebooks.services.lodgement.oss_q.mapping import alpha3_to_alpha2

    doc = yaml.safe_load(_SEED.read_text())
    seed_rates = {
        alpha3_to_alpha2(r["country_code"]): Decimal(str(r["standard_vat_rate_percent"]))
        for r in doc["rows"]
    }
    assert seed_rates == _EMBEDDED_STANDARD_RATES


def test_member_state_names_alpha2_all_present_in_seed_via_alpha3() -> None:
    doc = yaml.safe_load(_SEED.read_text())
    seed_alpha3 = {r["country_code"] for r in doc["rows"]}
    for alpha2 in MEMBER_STATE_NAMES:
        assert ALPHA2_TO_ALPHA3[alpha2] in seed_alpha3


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_oss_member_state_rates() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("EE", version_tag="test-oss-q-m2")
    assert "EE/oss_member_state_rates.yaml" in counts, (
        f"loader skipped the OSS member-state rate seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        rate = (
            await s.execute(
                text(
                    "SELECT standard_vat_rate_percent FROM oss_member_state_rates "
                    "WHERE country_code = 'DEU' AND effective_from = '2021-07-01'"
                )
            )
        ).scalar_one()
        assert rate == Decimal("19.0000")

        n = (
            await s.execute(text("SELECT count(*) FROM oss_member_state_rates"))
        ).scalar_one()
        assert n == 17, f"Expected 17 OSS member-state rates, got {n}"
