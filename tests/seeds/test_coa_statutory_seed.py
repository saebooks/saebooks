"""M1.5 · Wave 5-CoA (T10b) — statutory CoA framework + reporting-taxonomy
seed integrity and AU-parity proof.

Three test groups:
  * Pure-unit YAML checks (no DB) — the AU seeds parse, target the right
    tables, have unique codes, and every ``taxonomy_format`` is a real
    ``TAXONOMY_FORMATS`` value. Runs in the standard suite.
  * AU-parity unit check — Australia mandates no chart-of-accounts
    numbering plan, so every AU framework row must carry
    ``is_legally_mandated: false``.
  * Reference-DB integration (skipped unless the reference DB is
    configured, same gate as the loader test) — the seeds load, the AU
    rows resolve, and existing AU ``chart_template`` rows keep their new
    statutory columns NULL (the recommended AU chart is untouched).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saebooks.models.reference.reporting_taxonomy import TAXONOMY_FORMATS

_AU_DIR = (
    Path(__file__).resolve().parents[2]
    / "saebooks"
    / "seeds"
    / "jurisdictions"
    / "AU"
)
_FRAMEWORK_SEED = _AU_DIR / "statutory_account_frameworks.yaml"
_TAXONOMY_SEED = _AU_DIR / "reporting_taxonomies.yaml"


def test_au_statutory_framework_seed_valid() -> None:
    doc = yaml.safe_load(_FRAMEWORK_SEED.read_text())
    assert doc["table"] == "statutory_account_frameworks"
    assert doc["key"] == ["jurisdiction", "code"]
    rows = doc["rows"]
    assert rows, "AU statutory-framework seed is empty"

    codes = [r["code"] for r in rows]
    assert len(codes) == len(set(codes)), "duplicate framework codes in AU seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        # AU parity: Australia mandates no numbering plan, so no AU row may
        # claim to be legally mandated.
        assert r["is_legally_mandated"] is False, (
            f"AU framework {r['code']!r} claims to be legally mandated — "
            "Australia has no statutory chart-of-accounts numbering plan"
        )


def test_au_reporting_taxonomy_seed_valid() -> None:
    doc = yaml.safe_load(_TAXONOMY_SEED.read_text())
    assert doc["table"] == "reporting_taxonomies"
    assert doc["key"] == ["jurisdiction", "code"]
    rows = doc["rows"]
    assert rows, "AU reporting-taxonomy seed is empty"

    codes = [r["code"] for r in rows]
    assert len(codes) == len(set(codes)), "duplicate taxonomy codes in AU seed"

    for r in rows:
        assert r["jurisdiction"] == "AUS"
        assert r["taxonomy_format"] in TAXONOMY_FORMATS, (
            f"row {r['code']!r} has unknown taxonomy_format "
            f"{r['taxonomy_format']!r}"
        )

    # The taxonomy the live SBR lodgement path renders under must be present.
    assert "sbr_au" in codes


pytestmark_ref = pytest.mark.skipif(
    not os.environ.get("REFERENCE_MIGRATION_DATABASE_URL"),
    reason="REFERENCE_MIGRATION_DATABASE_URL not configured",
)


@pytestmark_ref
@pytest.mark.asyncio
async def test_load_coa_statutory_seeds() -> None:
    from sqlalchemy import text

    from saebooks.db import ReferenceMigrationSession
    from saebooks.services.reference.loader import load_seeds

    counts = await load_seeds("AU", version_tag="test-t10b")
    assert "AU/statutory_account_frameworks.yaml" in counts, (
        f"loader skipped the statutory-framework seed: {sorted(counts)}"
    )
    assert "AU/reporting_taxonomies.yaml" in counts, (
        f"loader skipped the reporting-taxonomy seed: {sorted(counts)}"
    )

    async with ReferenceMigrationSession() as s:
        # The AU framework row exists and is not legally mandated.
        mandated = (
            await s.execute(
                text(
                    "SELECT is_legally_mandated FROM statutory_account_frameworks "
                    "WHERE jurisdiction = 'AUS' AND code = 'general'"
                )
            )
        ).scalar_one()
        assert mandated is False

        # The SBR AU taxonomy row exists with an XBRL format.
        fmt = (
            await s.execute(
                text(
                    "SELECT taxonomy_format FROM reporting_taxonomies "
                    "WHERE jurisdiction = 'AUS' AND code = 'sbr_au'"
                )
            )
        ).scalar_one()
        assert fmt == "xbrl"

        # AU parity: the recommended AU chart is untouched — every AU
        # chart_template row keeps the new statutory columns NULL.
        tainted = (
            await s.execute(
                text(
                    "SELECT count(*) FROM chart_template "
                    "WHERE jurisdiction = 'AUS' AND ("
                    "statutory_framework_code IS NOT NULL "
                    "OR statutory_account_code IS NOT NULL "
                    "OR statutory_account_label_local IS NOT NULL "
                    "OR statutory_parent_class IS NOT NULL)"
                )
            )
        ).scalar_one()
        assert tainted == 0, (
            f"{tainted} AU chart_template rows unexpectedly carry statutory "
            "mapping data — AU mandates no framework"
        )
