"""M1.5 · T11 — income/corporate/capital/BIK canonical reference tables:
seed integrity + reference-DB load.

Eight tests (four seed-integrity + four reference-DB load), one per table:
  * ``test_au_capital_gains_tax_regime_seed_valid`` / ``test_load_capital_gains_tax_regimes``
  * ``test_au_corporate_tax_rate_seed_valid`` / ``test_load_corporate_tax_rates``
  * ``test_au_dividend_relief_mechanism_seed_valid`` / ``test_load_dividend_relief_mechanisms``
  * ``test_au_benefit_in_kind_rate_seed_valid`` / ``test_load_benefit_in_kind_rates``

The seed-integrity tests are pure-unit (no DB) and run in the standard
suite; the load tests are reference-DB integration, skipped unless
``REFERENCE_MIGRATION_DATABASE_URL`` is configured (same gate as
``test_load_entity_structures`` / the T7 loader tests).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference.benefit_in_kind_rate import (
    BENEFIT_IN_KIND_INCIDENCES,
    BENEFIT_IN_KIND_VALUATION_METHODS,
)
from saebooks.models.reference.capital_gains_tax_regime import CGT_RELIEF_MECHANISMS
from saebooks.models.reference.dividend_relief_mechanism import (
    DIVIDEND_RELIEF_MECHANISM_TYPES,
)

_SEED_DIR = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
)
_CGT_SEED = _SEED_DIR / "capital_gains_tax_regimes.yaml"
_CORP_TAX_SEED = _SEED_DIR / "corporate_tax_rates.yaml"
_DIVIDEND_SEED = _SEED_DIR / "dividend_relief_mechanisms.yaml"
_BIK_SEED = _SEED_DIR / "benefit_in_kind_rates.yaml"


def test_au_capital_gains_tax_regime_seed_valid() -> None:
    doc = yaml.safe_load(_CGT_SEED.read_text())
    assert doc["table"] == "capital_gains_tax_regimes"
    rows = doc["rows"]
    assert rows, "AU capital-gains-tax-regimes seed is empty"

    keys = [(r["jurisdiction"], r["relief_mechanism"], r["effective_from"]) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (jurisdiction, relief_mechanism, effective_from) in AU seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["relief_mechanism"] in CGT_RELIEF_MECHANISMS, (
            f"row {r['relief_mechanism']!r} is not a known CGT relief mechanism"
        )

    by_mechanism = {r["relief_mechanism"]: r for r in rows}
    assert by_mechanism["discount"]["holding_period_threshold_days"] == 365
    assert by_mechanism["discount"]["relief_rate_or_schedule"]["rate_percent"] == 50.0


def test_au_corporate_tax_rate_seed_valid() -> None:
    doc = yaml.safe_load(_CORP_TAX_SEED.read_text())
    assert doc["table"] == "corporate_tax_rates"
    rows = doc["rows"]
    assert rows, "AU corporate-tax-rates seed is empty"

    keys = [
        (r["jurisdiction"], r.get("sub_jurisdiction"), r["tax_year"], r["entity_scope"])
        for r in rows
    ]
    assert len(keys) == len(set(keys)), "duplicate (jurisdiction, sub_jurisdiction, tax_year, entity_scope) in AU seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["rate_percent"] > 0

    by_scope = {r["entity_scope"]: r for r in rows}
    assert by_scope["base_rate_entity"]["rate_percent"] == 25.0
    assert by_scope["standard"]["rate_percent"] == 30.0


def test_au_dividend_relief_mechanism_seed_valid() -> None:
    doc = yaml.safe_load(_DIVIDEND_SEED.read_text())
    assert doc["table"] == "dividend_relief_mechanisms"
    rows = doc["rows"]
    assert rows, "AU dividend-relief-mechanisms seed is empty"

    keys = [(r["jurisdiction"], r["mechanism_type"], r["effective_from"]) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (jurisdiction, mechanism_type, effective_from) in AU seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["mechanism_type"] in DIVIDEND_RELIEF_MECHANISM_TYPES, (
            f"row {r['mechanism_type']!r} is not a known dividend relief mechanism type"
        )

    by_type = {r["mechanism_type"]: r for r in rows}
    assert by_type["franking"]["refundable"] is True
    assert by_type["franking"]["credit_or_exemption_rate"] == 30.0


def test_au_benefit_in_kind_rate_seed_valid() -> None:
    doc = yaml.safe_load(_BIK_SEED.read_text())
    assert doc["table"] == "benefit_in_kind_rates"
    rows = doc["rows"]
    assert rows, "AU benefit-in-kind-rates seed is empty"

    keys = [(r["jurisdiction"], r["benefit_category"], r["effective_from"]) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (jurisdiction, benefit_category, effective_from) in AU seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["incidence"] in BENEFIT_IN_KIND_INCIDENCES, (
            f"row {r['benefit_category']!r} has unknown incidence {r['incidence']!r}"
        )
        assert r["valuation_method"] in BENEFIT_IN_KIND_VALUATION_METHODS, (
            f"row {r['benefit_category']!r} has unknown valuation_method {r['valuation_method']!r}"
        )
        assert 1 <= r["filing_period_start_month"] <= 12
        assert 1 <= r["filing_period_end_month"] <= 12

    by_category = {r["benefit_category"]: r for r in rows}
    # AU generalises FBT as employer_taxed (unlike jurisdictions that tax
    # the employee on the benefit's value) — the property the audit named.
    assert by_category["general"]["incidence"] == "employer_taxed"


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_capital_gains_tax_regimes() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t11")
    assert "AU/capital_gains_tax_regimes.yaml" in counts, (
        f"loader skipped the capital-gains-tax-regimes seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        row = (
            await s.execute(
                text(
                    "SELECT holding_period_threshold_days, relief_rate_or_schedule "
                    "FROM capital_gains_tax_regimes "
                    "WHERE jurisdiction = 'AUS' AND relief_mechanism = 'discount'"
                )
            )
        ).one()
        assert row.holding_period_threshold_days == 365
        assert row.relief_rate_or_schedule["rate_percent"] == 50.0


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_corporate_tax_rates() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t11")
    assert "AU/corporate_tax_rates.yaml" in counts, (
        f"loader skipped the corporate-tax-rates seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        rate = (
            await s.execute(
                text(
                    "SELECT rate_percent FROM corporate_tax_rates "
                    "WHERE jurisdiction = 'AUS' AND tax_year = 2026 "
                    "AND entity_scope = 'base_rate_entity'"
                )
            )
        ).scalar_one()
        assert rate == 25.0


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_dividend_relief_mechanisms() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t11")
    assert "AU/dividend_relief_mechanisms.yaml" in counts, (
        f"loader skipped the dividend-relief-mechanisms seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        refundable = (
            await s.execute(
                text(
                    "SELECT refundable FROM dividend_relief_mechanisms "
                    "WHERE jurisdiction = 'AUS' AND mechanism_type = 'franking'"
                )
            )
        ).scalar_one()
        assert refundable is True


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_benefit_in_kind_rates() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t11")
    assert "AU/benefit_in_kind_rates.yaml" in counts, (
        f"loader skipped the benefit-in-kind-rates seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        incidence = (
            await s.execute(
                text(
                    "SELECT incidence FROM benefit_in_kind_rates "
                    "WHERE jurisdiction = 'AUS' AND benefit_category = 'general'"
                )
            )
        ).scalar_one()
        assert incidence == "employer_taxed"
