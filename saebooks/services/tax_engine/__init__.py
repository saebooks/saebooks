"""Tax-engine dispatcher — per-jurisdiction strategy modules.

Public surface
--------------

* ``TaxEngine`` — runtime-checkable Protocol every per-jurisdiction
  implementation satisfies.
* ``get_engine(jurisdiction)`` — registry dispatcher; returns the
  engine for the named jurisdiction or raises ``NotImplementedError``
  for stubs (NZ/UK in M0; EE landed KMD-formula support Packet 3).
* ``PostingContext`` / ``TaxTreatment`` / ``ValidationError`` — shared
  data classes (re-exported from ``types``).

Adding a new jurisdiction
-------------------------

1. Implement the engine in a new module (e.g. ``nz.py``).
2. Register it in ``_REGISTRY`` here.

The protocol is duck-typed at runtime via ``isinstance`` — but
practically every engine subclasses or composes from a base helper
class to get sensible defaults for ``validate`` (returns ``[]``) and
``boxes`` (returns ``{}``).

Per-jurisdiction posting dispatcher (KMD-formula support Packet 3)
--------------------------------------------------------------------
``services.journal._apply_tax_treatment`` used to hardcode
``get_engine("AU")`` for every post (the M0 docstring's "until then
every post is AU"). It now resolves ``Company.jurisdiction`` and calls
``get_engine(...)`` here — this module was already the intended seam
(the registry + stub pattern existed since M0), so the prerequisite the
KMD-formula-support scope's §3.4 point 1 describes was "plumb the
caller", not "build the dispatcher" — this module already was one.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from saebooks.services.tax_engine.types import (
    PeriodWindow,
    PostingContext,
    TaxTreatment,
    ValidationError,
)

if TYPE_CHECKING:
    from decimal import Decimal


@runtime_checkable
class TaxEngine(Protocol):
    """Per-jurisdiction tax determination + reporting interface.

    Methods are sync because none of them issue I/O — the engine works
    against in-memory ``PostingContext`` objects and against query
    results passed in by the caller. The caller (router or service)
    is responsible for any DB work.
    """

    jurisdiction: str

    def compute(self, ctx: PostingContext) -> TaxTreatment:
        """Determine the tax treatment for one journal line.

        Deterministic: same ``ctx`` → same ``TaxTreatment`` forever.
        The result is snapshotted onto the line so audit history
        survives later changes to the underlying tax_code rows.
        """
        ...

    def compute_components(self, ctx: PostingContext) -> list[TaxTreatment]:
        """Determine EVERY tax component for one journal line (Packet 3,
        KMD-formula support scope §3.4 point 2).

        Most lines produce exactly one component — the same
        ``TaxTreatment`` ``compute()`` returns, wrapped in a
        single-element list; every engine implements this trivially as
        ``[self.compute(ctx)]`` unless it has a genuine multi-component
        need (e.g. EE's reverse-charge output+input fan-out — one
        EU-acquisition purchase line self-assesses BOTH an output-role
        component, feeding the sale-side box, and an input-role
        component, feeding the deductible-input box).

        ``services.journal._apply_tax_treatment`` calls this method
        (never ``compute`` directly) and materialises one
        ``JournalLineTaxComponent`` row per returned treatment, in list
        order (``sequence``). The FIRST element is also what gets
        snapshotted onto ``journal_lines.tax_treatment`` (the JSONB
        column stays single-valued — for a reverse-charge line that is
        the output-role component).
        """
        ...

    def boxes(self, period: Any) -> dict[str, Decimal]:
        """Return the form-box mapping for a closed period.

        Keys are jurisdiction-specific labels: BAS labels for AU
        ("G1", "G2", "1A"...); VAT100 boxes for UK ("Box1"...);
        GST101 lines for NZ. Values are the period totals.
        """
        ...

    def validate(self, invoice: Any) -> list[ValidationError]:
        """Pre-post validation; return errors (empty list = clean)."""
        ...


def _au_factory() -> TaxEngine:
    # Local import to avoid pulling AU code on every import of this
    # package — we want jurisdictions to be loadable independently.
    from saebooks.services.tax_engine.au import AUTaxEngine

    return AUTaxEngine()


def _ee_factory() -> TaxEngine:
    # KMD-formula support Packet 3 — EE is no longer a stub. Local
    # import for the same reason as AU above.
    from saebooks.services.tax_engine.ee import EETaxEngine

    return EETaxEngine()


def _stub(jurisdiction: str, milestone: str):
    def _factory() -> TaxEngine:
        raise NotImplementedError(
            f"{jurisdiction} tax engine — implemented in {milestone}"
        )

    return _factory


_REGISTRY: dict[str, Any] = {
    "AU": _au_factory,
    "NZ": _stub("NZ", "M1"),
    "UK": _stub("UK", "M2"),
    "EE": _ee_factory,
}


def get_engine(jurisdiction: str) -> TaxEngine:
    """Return the engine for a jurisdiction.

    Raises ``KeyError`` for an unknown jurisdiction code, and
    ``NotImplementedError`` for stub jurisdictions registered but not
    yet built (NZ in M1, UK in M2). EE landed KMD-formula support
    Packet 3 — no longer a stub.
    """
    factory = _REGISTRY.get(jurisdiction)
    if factory is None:
        raise KeyError(
            f"Unknown jurisdiction {jurisdiction!r}. "
            f"Known: {sorted(_REGISTRY)}"
        )
    return factory()


__all__ = [
    "PeriodWindow",
    "PostingContext",
    "TaxEngine",
    "TaxTreatment",
    "ValidationError",
    "get_engine",
]
