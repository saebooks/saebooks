"""EE KMD (VAT return) file-export generators — e-MTA manual-upload path.

Engine-side XML/CSV generation only (scope §5: X-Road transmission is a
separate, proprietary adapter — ``services/lodgement/adapters/ee.py``,
untouched by this package). See ``mapping`` and ``serializer`` module
docstrings for the PLACEHOLDER-pinned-by-golden-file conformance note.
"""
from __future__ import annotations

from saebooks.services.lodgement.kmd.mapping import (
    KMD_BOX_ORDER,
    KMD_FIELD_NAMES,
    KMD_SCHEMA_REF,
    KMD_TAXONOMY_NS,
)
from saebooks.services.lodgement.kmd.serializer import (
    KmdFigures,
    KmdReportingContext,
    build_kmd_csv_document,
    build_kmd_xml_document,
)

__all__ = [
    "KMD_BOX_ORDER",
    "KMD_FIELD_NAMES",
    "KMD_SCHEMA_REF",
    "KMD_TAXONOMY_NS",
    "KmdFigures",
    "KmdReportingContext",
    "build_kmd_csv_document",
    "build_kmd_xml_document",
]
