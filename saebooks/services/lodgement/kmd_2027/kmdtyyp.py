"""KMDTYYP2026ap classification loader — the engine↔leaf mapping.

Loads ``seeds/jurisdictions/EE/kmdtyyp_mapping.yaml`` (a flat map, NOT a
reference-DB table — build-plan §4.3) once at import and exposes a pure lookup
API for the 2027 data-based KMD exporter:

* ``resolve_kmdtyyp(reporting_type, role)`` — the generator's forward map:
  a box-engine ``TaxCode.reporting_type`` + a ``role`` → a KMDTYYP2026ap leaf
  code, or ``None`` when the pair has no confident leaf (never guessed).
* ``leaf_meta(code)`` / ``LEAVES`` — per-leaf metadata (amount basis, koondvaade
  reconcile group, English label) used by the serializer and reconcile.py.
* ``is_unmapped_engine_tag(reporting_type, role)`` — engine tags the generator
  will encounter but must FLAG rather than classify.
* ``coverage()`` — the mapped-vs-unmapped leaf census (the risk metric).

Pure module: no DB, no I/O beyond the one-time YAML read. Safe to import from
serializer / reconcile / generator alike.

READY FOR the 2027 data-based KMD; NOT "compliant with" (VTK-stage law).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

# ``role`` discriminates the same reporting_type across the sale / acquisition /
# input / accounting sides — the reason a flat reporting_type→leaf map is wrong
# (``standard`` on a sale → M_101; on a deductible purchase → O_101).
Role = Literal["sale", "acquisition", "input", "accounting"]
AmountBasis = Literal["taxable_value", "input_vat"]

_SEED_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "seeds"
    / "jurisdictions"
    / "EE"
    / "kmdtyyp_mapping.yaml"
)


@dataclass(frozen=True)
class KmdTyypLeaf:
    """One selectable KMDTYYP2026ap leaf code + its engine mapping."""

    code: str
    name_en: str
    amount_basis: AmountBasis
    reconcile_group: str | None
    # (reporting_type, role) pairs that map UNAMBIGUOUSLY to this leaf; empty
    # when the leaf has no confident engine source today.
    engine_sources: tuple[tuple[str, str], ...]

    @property
    def is_mapped(self) -> bool:
        return bool(self.engine_sources)


@dataclass(frozen=True)
class _Loaded:
    leaves: dict[str, KmdTyypLeaf]
    forward: dict[tuple[str, str], str]          # (reporting_type, role) -> leaf code
    unmapped_tags: frozenset[tuple[str, str]]    # (reporting_type, role) the generator must flag
    classifier: str
    classifier_version: str
    section: str


class KmdTyypMappingError(ValueError):
    """The KMDTYYP seed is internally inconsistent (duplicate forward key,
    unknown reconcile field, etc.) — raised at load so a bad seed fails loud
    rather than silently misclassifying a filed transaction."""


@lru_cache(maxsize=1)
def _load() -> _Loaded:
    with _SEED_PATH.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise KmdTyypMappingError(f"{_SEED_PATH}: top-level YAML must be a mapping")

    leaves: dict[str, KmdTyypLeaf] = {}
    forward: dict[tuple[str, str], str] = {}
    raw_leaves = doc.get("leaves") or {}
    for code, meta in raw_leaves.items():
        basis = meta.get("amount_basis")
        if basis not in ("taxable_value", "input_vat"):
            raise KmdTyypMappingError(
                f"{code}: amount_basis must be 'taxable_value' or 'input_vat', got {basis!r}"
            )
        sources: list[tuple[str, str]] = []
        for src in meta.get("engine") or []:
            rt = src.get("reporting_type")
            role = src.get("role")
            if not rt or role not in ("sale", "acquisition", "input", "accounting"):
                raise KmdTyypMappingError(
                    f"{code}: bad engine source {src!r} (need reporting_type + valid role)"
                )
            key = (rt, role)
            if key in forward:
                raise KmdTyypMappingError(
                    f"{code}: engine source {key} already maps to {forward[key]!r} — "
                    "a (reporting_type, role) pair must map to exactly one leaf"
                )
            forward[key] = code
            sources.append(key)
        leaves[code] = KmdTyypLeaf(
            code=code,
            name_en=meta.get("name_en", ""),
            amount_basis=basis,
            reconcile_group=meta.get("reconcile_group"),
            engine_sources=tuple(sources),
        )

    unmapped: set[tuple[str, str]] = set()
    for tag in doc.get("unmapped_engine_tags") or []:
        rt = tag.get("reporting_type")
        role = tag.get("role")
        if rt and role:
            key = (rt, role)
            if key in forward:
                raise KmdTyypMappingError(
                    f"unmapped_engine_tags lists {key} but it is also mapped to "
                    f"leaf {forward[key]!r} — a tag cannot be both"
                )
            unmapped.add(key)

    return _Loaded(
        leaves=leaves,
        forward=forward,
        unmapped_tags=frozenset(unmapped),
        classifier=doc.get("classifier", "KMDTYYP2026ap"),
        classifier_version=str(doc.get("classifier_version", "")),
        section=doc.get("section", "EE0203001"),
    )


# ---- Public pure API --------------------------------------------------------

def classifier_name() -> str:
    """The ``gl-cor:accountSubType`` token every EE0203001 row carries."""
    return _load().classifier


def section_code() -> str:
    """The ``gl-cor:entryNumber`` data-section code (``EE0203001``)."""
    return _load().section


def resolve_kmdtyyp(reporting_type: str, role: Role) -> str | None:
    """Map a box-engine ``(reporting_type, role)`` to a KMDTYYP2026ap leaf code.

    Returns ``None`` when the pair has no confident leaf — the generator must
    then surface a data-quality flag, NEVER guess a code (build-plan §4.5)."""
    return _load().forward.get((reporting_type, role))


def is_unmapped_engine_tag(reporting_type: str, role: Role) -> bool:
    """True for a ``(reporting_type, role)`` the exporter knows it cannot yet
    classify (listed in ``unmapped_engine_tags``) — the generator flags these
    explicitly rather than dropping the transaction silently."""
    return (reporting_type, role) in _load().unmapped_tags


def leaf_meta(code: str) -> KmdTyypLeaf | None:
    """Per-leaf metadata, or ``None`` if ``code`` is not a known leaf."""
    return _load().leaves.get(code)


def is_valid_leaf(code: str) -> bool:
    return code in _load().leaves


def all_leaves() -> dict[str, KmdTyypLeaf]:
    """A copy of every known leaf keyed by code (57 selectable level-3 codes)."""
    return dict(_load().leaves)


def coverage() -> dict[str, object]:
    """The risk metric (build-plan top-5 #1): how many KMDTYYP leaves have a
    confident engine source vs how many are UNMAPPED, plus the unmapped list."""
    leaves = _load().leaves
    mapped = sorted(c for c, leaf in leaves.items() if leaf.is_mapped)
    unmapped = sorted(c for c, leaf in leaves.items() if not leaf.is_mapped)
    return {
        "total_leaves": len(leaves),
        "mapped_count": len(mapped),
        "unmapped_count": len(unmapped),
        "mapped": mapped,
        "unmapped": unmapped,
    }
