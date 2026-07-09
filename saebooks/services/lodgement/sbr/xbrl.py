"""XBRL instance builder for SBR (Standard Business Reporting) documents —
COMMUNITY EDITION STUB.

The ATO (and most SBR-based regulators) lodge Activity Statements and other
returns as an XBRL payload. Building an ATO-conformant, EVTE-validated XBRL
instance (the real taxonomy QNames, target namespace, and schemaRef, per the
SBR Message Implementation Guide) is a commercial SAE Books feature — see
CHARTER.md / LICENSING.md. This module keeps the data-holding shapes
(``ReportingContext``, ``Fact``) and the ``XbrlInstance`` builder's
call-surface so callers/tests can still construct these objects, but the
actual document serialisers (``build_instance``, ``XbrlInstance.to_bytes``)
raise ``NotImplementedError`` in this edition.

Boundary: even in the commercial edition, this module produces the
*business document* only. Signing with the ATO Machine Credential and the
ebMS3/AS4 transport to the ATO happen in the private commercial
lodge-server (its API contract is private, not part of this repository),
which receives this document as ``envelope_xml`` (base64) + ``envelope_hash``.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ReportingContext:
    """The single XBRL context every fact in an Activity Statement shares."""

    abn: str
    period_start: date
    period_end: date
    context_id: str = "ctx-period"
    unit_id: str = "AUD"


@dataclass(frozen=True)
class Fact:
    """One reported value: a taxonomy concept (ns + local name) and its amount."""

    concept_ns: str
    concept_name: str
    value: Decimal
    decimals: str = "0"  # AS monetary labels are whole dollars


_LODGEMENT_STUB_MESSAGE = (
    "Certified e-lodgement is a commercial SAE Books feature; the community "
    "edition ships box definitions + the return calculator but not the "
    "regulator transmission adapters. See CHARTER.md / LICENSING.md."
)


def build_instance(
    facts: list[Fact],
    ctx: ReportingContext,
    *,
    taxonomy_ns: str,
    taxonomy_prefix: str,
    schema_ref: str,
) -> bytes:
    """Build an XBRL instance and return pretty-printed UTF-8 bytes.

    COMMUNITY EDITION STUB — always raises. See module docstring.
    """
    raise NotImplementedError(_LODGEMENT_STUB_MESSAGE)


def _amount(value: Decimal, decimals: str) -> str:
    """Format a monetary value to ``decimals`` places (e.g. "2" for STP cents)."""
    places = int(decimals)
    q = Decimal(1) if places <= 0 else Decimal(1).scaleb(-places)
    return str(Decimal(value).quantize(q))


@dataclass
class _BuilderContext:
    context_id: str
    abn: str
    period_start: date
    period_end: date


class XbrlInstance:
    """Builder for an XBRL instance with one-or-more contexts and mixed facts.

    Unlike ``build_instance`` (single context, all-monetary — used by BAS),
    this supports multiple contexts (e.g. PAYEVNT employer context + one per
    payee) and both monetary facts (``add_money`` → carries ``unitRef`` +
    ``decimals``) and non-monetary item facts (``add_text`` → no unit, for
    names/dates/codes; XBRL forbids a unit on non-numeric items).
    """

    def __init__(
        self, *, taxonomy_ns: str, taxonomy_prefix: str, schema_ref: str, unit_id: str = "AUD"
    ) -> None:
        self.taxonomy_ns = taxonomy_ns
        self.taxonomy_prefix = taxonomy_prefix
        self.schema_ref = schema_ref
        self.unit_id = unit_id
        self._contexts: dict[str, _BuilderContext] = {}
        # facts: (concept_name, text, context_id, is_monetary, decimals|None)
        self._facts: list[tuple[str, str, str, bool, str | None]] = []
        self._any_monetary = False

    def add_context(
        self, context_id: str, *, abn: str, period_start: date, period_end: date
    ) -> str:
        self._contexts[context_id] = _BuilderContext(context_id, abn, period_start, period_end)
        return context_id

    def add_money(
        self, concept_name: str, value: Any, *, context_id: str, decimals: str = "2"
    ) -> None:
        if value is None:
            return
        self._any_monetary = True
        self._facts.append((concept_name, _amount(Decimal(str(value)), decimals), context_id, True, decimals))

    def add_text(self, concept_name: str, value: Any, *, context_id: str) -> None:
        if value is None or value == "":
            return
        self._facts.append((concept_name, str(value), context_id, False, None))

    def to_bytes(self) -> bytes:
        """Serialise the accumulated contexts/facts to an XBRL instance.

        COMMUNITY EDITION STUB — always raises. ``add_context`` / ``add_money``
        / ``add_text`` above stay usable so callers can assemble a document
        model, but rendering it to an ATO-conformant XBRL instance is a
        commercial SAE Books feature. See module docstring.
        """
        raise NotImplementedError(_LODGEMENT_STUB_MESSAGE)


def envelope_parts(document: bytes) -> tuple[str, str]:
    """Return ``(envelope_b64, envelope_sha256_hex)`` for the lodge-server body.

    Mirrors ``services/lodgement/remote.py:_envelope_payload`` so the hash and
    the base64 are always derived from the same bytes.
    """
    return (
        base64.b64encode(document).decode("ascii"),
        hashlib.sha256(document).hexdigest(),
    )
