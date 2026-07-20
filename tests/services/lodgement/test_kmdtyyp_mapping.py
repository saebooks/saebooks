"""KMDTYYP2026ap mapping seed — coverage + integrity (pure, no DB).

The KMDTYYP mapping is Module 4's risk concentration (build-plan top-5 #1):
~57 hierarchical leaf codes vs the box engine's ~20 coarser reporting_type tags.
These tests pin the coverage census (mapped vs UNMAPPED), assert no orphan /
double-mapped engine tag, and prove the sample's codes are all valid leaves.
"""
from __future__ import annotations

import yaml

from saebooks.services.lodgement.kmd_2027 import kmdtyyp

# Every box-engine EE reporting_type that reaches a KMD box (the union of
# tax_return_box_definitions.yaml feeder_tax_codes) — the tags the exporter
# must have a defensible disposition for (map or explicitly flag).
_ENGINE_REPORTING_TYPES = {
    "standard", "standard_legacy_20", "standard_legacy_22",
    "reduced_9", "reduced_13", "reduced_5_legacy",
    "zero_ic_goods", "zero_ic_services", "zero_export", "zero_traveller",
    "exempt", "capital", "input_import",
    "rc_eu_acq_goods", "rc_eu_acq_services", "rc_domestic_acq",
    "rc_domestic_supply", "ic_acq_exempt", "ee_acq_foreign", "install_other_ms",
}


def test_leaf_count_is_57() -> None:
    """The seed carries every selectable level-3 leaf from the taxonomy sheet."""
    assert len(kmdtyyp.all_leaves()) == 57


def test_coverage_census_is_18_of_57() -> None:
    """The honest coverage number: 18 leaves have a confident LIVE engine source,
    39 do not. Of the 39, 11 are TIER-2 (a classifier tag away — carried in the
    seed's per-leaf `engine_pending`, deliberately NOT loaded into the forward map)
    and 28 are TIER-3 (need a genuine engine feature: margin-scheme accounting,
    triangular tracking, cash-accounting, passenger-car 50/100%, partial deduction,
    warehousing/import-amendment, correction, accounting-entry path). The
    2026-07-11 pass added M_302 via the existing unclaimed `out_of_scope` tag.
    If this changes, update the seed header + ~/records/saebooks/kmdtyyp-mapping-tiers.md
    — the number is a deliberate, reviewed figure."""
    cov = kmdtyyp.coverage()
    assert cov["total_leaves"] == 57
    assert cov["mapped_count"] == 18
    assert cov["unmapped_count"] == 39
    assert set(cov["mapped"]) == {
        "M_101", "M_105", "M_201", "M_202", "M_208", "M_209", "M_301", "M_302", "M_304",
        "S_101", "S_102", "S_105", "S_106",
        "O_101", "O_106", "O_201", "O_401", "O_402",
    }


def test_out_of_scope_sale_maps_to_m302_not_supply() -> None:
    """TIER-1 (2026-07-11): the existing, unclaimed `out_of_scope` engine tag on
    the sale side is M_302 'not supply' — distinct from `exempt`->M_301 (an
    in-scope but exempt supply). This is the only clean existing-tag win: every
    other reachable acquisition/input tag is already claimed."""
    assert kmdtyyp.resolve_kmdtyyp("out_of_scope", "sale") == "M_302"
    assert kmdtyyp.resolve_kmdtyyp("exempt", "sale") == "M_301"


def test_reverse_map_is_a_function() -> None:
    """HARD invariant: the reverse map (reporting_type, role) -> leaf is a FUNCTION.
    No tuple may resolve to two leaves, or the generator's classification is
    ambiguous. The loader enforces this at load for the LIVE `engine:` map (it
    raises KmdTyypMappingError on a duplicate forward key); this test pins the
    resulting invariant AND extends it to the proposed `engine_pending` tuples so a
    future Tier-2 promotion can never introduce a collision."""
    # The live loader already guarantees the LIVE map is a function (it would have
    # raised at import otherwise) — a dict's keys are unique, so forward being
    # single-valued is the invariant; assert it is populated as expected.
    loaded = kmdtyyp._load()
    assert loaded.forward, "forward map unexpectedly empty"

    # Global uniqueness across LIVE engine + proposed engine_pending + flagged
    # unmapped_engine_tags: one (reporting_type, role) may name at most one leaf.
    doc = yaml.safe_load(kmdtyyp._SEED_PATH.open(encoding="utf-8"))
    owner: dict[tuple[str, str], str] = {}
    for code, meta in doc["leaves"].items():
        for kind in ("engine", "engine_pending"):
            for src in meta.get(kind) or []:
                key = (src["reporting_type"], src["role"])
                assert key not in owner, (
                    f"(reporting_type, role) {key} maps to both {owner[key]!r} and "
                    f"{code!r} — the reverse map must be a function"
                )
                owner[key] = code
    for tag in doc.get("unmapped_engine_tags") or []:
        key = (tag["reporting_type"], tag["role"])
        assert key not in owner, (
            f"flagged unmapped tag {key} is also mapped to {owner[key]!r}"
        )


def test_every_engine_tag_has_a_disposition() -> None:
    """Each box-engine reporting_type either maps to a leaf on at least one role
    (sale/acquisition/input) OR is explicitly listed as an unmapped tag — never
    silently unhandled (build-plan risk #1 mitigation)."""
    for rt in _ENGINE_REPORTING_TYPES:
        mapped = any(
            kmdtyyp.resolve_kmdtyyp(rt, role) is not None
            for role in ("sale", "acquisition", "input", "accounting")
        )
        flagged = any(
            kmdtyyp.is_unmapped_engine_tag(rt, role)
            for role in ("sale", "acquisition", "input", "accounting")
        )
        assert mapped or flagged, f"reporting_type {rt!r} has no mapping and is not flagged"


def test_ee_acq_foreign_is_flagged_not_guessed() -> None:
    """The one ordinary-posting tag we deliberately do NOT map (ambiguous across
    S_103/S_104) is flagged so the generator surfaces it."""
    assert kmdtyyp.resolve_kmdtyyp("ee_acq_foreign", "acquisition") is None
    assert kmdtyyp.is_unmapped_engine_tag("ee_acq_foreign", "acquisition")


def test_role_discriminates_standard_tag() -> None:
    """The (reporting_type, role) key is load-bearing: `standard` on a sale is a
    different leaf than on a purchase."""
    assert kmdtyyp.resolve_kmdtyyp("standard", "sale") == "M_101"
    assert kmdtyyp.resolve_kmdtyyp("standard", "input") == "O_101"


def test_reverse_charge_acquisition_maps_both_components() -> None:
    """An EU acquisition produces an S_ base + an O_ input leaf — the sample's
    Example 14 (S_101) / Example 17 (O_401) pairing."""
    assert kmdtyyp.resolve_kmdtyyp("rc_eu_acq_goods", "acquisition") == "S_101"
    assert kmdtyyp.resolve_kmdtyyp("rc_eu_acq_goods", "input") == "O_401"
    assert kmdtyyp.resolve_kmdtyyp("rc_eu_acq_services", "acquisition") == "S_102"
    assert kmdtyyp.resolve_kmdtyyp("rc_eu_acq_services", "input") == "O_402"


def test_sample_codes_are_all_valid_leaves() -> None:
    """Every KMDTYYP code that appears in the official package sample is a known
    selectable leaf in the seed."""
    for code in (
        "M_101", "M_103", "M_104", "M_105", "M_201", "M_206", "M_208", "M_210",
        "M_301", "S_101", "O_101", "O_106", "O_401", "O_601",
    ):
        assert kmdtyyp.is_valid_leaf(code), f"sample code {code} missing from seed"


def test_amount_basis_is_consistent_with_prefix() -> None:
    """M_/S_ leaves carry taxable value; O_ leaves carry input VAT (the
    andmepohine read §1 rule)."""
    for code, leaf in kmdtyyp.all_leaves().items():
        if code.startswith(("M_", "S_", "A_")):
            assert leaf.amount_basis == "taxable_value", code
        elif code.startswith("O_"):
            assert leaf.amount_basis == "input_vat", code
