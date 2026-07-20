"""TSD (income + social + withholding tax return) — listing generator +
file serializer.

Packet 4 (``generator.py``): MAIN totals + Lisa-1 row-set generator
from posted EE pay runs. Packet 5 (``mapping.py``/``serializer.py``,
mirroring ``services/lodgement/kmd_inf/``): the e-MTA XML/CSV file
serializer + ``tax_returns`` persistence.

Module 1 (ee-frontier-build-plan.md §"MODULE 1"): Lisa 2-7 row/aggregate
dataclasses + XML (and, where a real CSV spec exists, CSV) serializer
support, extending the same ``tsd/`` package. See ``generator.py``'s
Module-1 section docstring for the buildable-now boundary (serializer +
mapping shippable now; the `generate_tsd_lisaN` assembly functions are
gated ``NotImplementedError`` stubs pending EE source-data models the
engine does not hold yet).
"""
from __future__ import annotations

from saebooks.services.lodgement.tsd.generator import (
    PAYMENT_TYPE_WAGES,
    TsdDataQualityError,
    TsdLisa1Row,
    # Module 1 — Lisa 2-7
    TsdLisa2ARow,
    TsdLisa2BRow,
    TsdLisa2InvFondRow,
    TsdLisa2Listing,
    TsdLisa2MvtRow,
    TsdLisa2Totals,
    TsdLisa3Header,
    TsdLisa4Header,
    TsdLisa5Header,
    TsdLisa6Header,
    TsdLisa6Listing,
    TsdLisa6Row1,
    TsdLisa6Row2,
    TsdLisa6Row3,
    TsdLisa7Header,
    TsdLisa7Listing,
    TsdLisa7Row1b,
    TsdLisa7Row1C,
    TsdLisa7Row2,
    TsdLisa7Row2B,
    TsdLisa7Row4,
    TsdListing,
    TsdMainTotals,
    compute_lisa2_totals,
    generate_tsd,
    generate_tsd_lisa2,
    generate_tsd_lisa3,
    generate_tsd_lisa4,
    generate_tsd_lisa5,
    generate_tsd_lisa6,
    generate_tsd_lisa7,
)
from saebooks.services.lodgement.tsd.mapping import (
    TSD_MAIN_ELEMENTS,
    TSD_PAYMENT_TYPE_MAP,
    TSD_ROOT_ELEMENT,
    TSD_VM_ELEMENTS,
)
from saebooks.services.lodgement.tsd.serializer import (
    TsdReportingContext,
    build_tsd_lisa1_csv_document,
    build_tsd_lisa2_a_csv_document,
    build_tsd_lisa2_xml_document,
    build_tsd_lisa3_xml_document,
    build_tsd_lisa4_xml_document,
    build_tsd_lisa5_xml_document,
    build_tsd_lisa6_xml_document,
    build_tsd_lisa7_xml_document,
    build_tsd_main_csv_document,
    build_tsd_xml_document,
    persist_tsd_return,
)

__all__ = [
    "PAYMENT_TYPE_WAGES",
    "TSD_MAIN_ELEMENTS",
    "TSD_PAYMENT_TYPE_MAP",
    "TSD_ROOT_ELEMENT",
    "TSD_VM_ELEMENTS",
    "TsdDataQualityError",
    "TsdLisa1Row",
    # Module 1 — Lisa 2-7
    "TsdLisa2ARow",
    "TsdLisa2BRow",
    "TsdLisa2InvFondRow",
    "TsdLisa2Listing",
    "TsdLisa2MvtRow",
    "TsdLisa2Totals",
    "TsdLisa3Header",
    "TsdLisa4Header",
    "TsdLisa5Header",
    "TsdLisa6Header",
    "TsdLisa6Listing",
    "TsdLisa6Row1",
    "TsdLisa6Row2",
    "TsdLisa6Row3",
    "TsdLisa7Header",
    "TsdLisa7Listing",
    "TsdLisa7Row1C",
    "TsdLisa7Row1b",
    "TsdLisa7Row2",
    "TsdLisa7Row2B",
    "TsdLisa7Row4",
    "TsdListing",
    "TsdMainTotals",
    "TsdReportingContext",
    "build_tsd_lisa1_csv_document",
    "build_tsd_lisa2_a_csv_document",
    "build_tsd_lisa2_xml_document",
    "build_tsd_lisa3_xml_document",
    "build_tsd_lisa4_xml_document",
    "build_tsd_lisa5_xml_document",
    "build_tsd_lisa6_xml_document",
    "build_tsd_lisa7_xml_document",
    "build_tsd_main_csv_document",
    "build_tsd_xml_document",
    "compute_lisa2_totals",
    "generate_tsd",
    "generate_tsd_lisa2",
    "generate_tsd_lisa3",
    "generate_tsd_lisa4",
    "generate_tsd_lisa5",
    "generate_tsd_lisa6",
    "generate_tsd_lisa7",
    "persist_tsd_return",
]
