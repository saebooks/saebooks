"""TSD (income + social + withholding tax return) — listing generator +
file serializer.

Packet 4 (``generator.py``): MAIN totals + Lisa-1 row-set generator
from posted EE pay runs. Packet 5 (``mapping.py``/``serializer.py``,
mirroring ``services/lodgement/kmd_inf/``): the e-MTA XML/CSV file
serializer + ``tax_returns`` persistence.
"""
from __future__ import annotations

from saebooks.services.lodgement.tsd.generator import (
    PAYMENT_TYPE_WAGES,
    TsdDataQualityError,
    TsdLisa1Row,
    TsdListing,
    TsdMainTotals,
    generate_tsd,
)
from saebooks.services.lodgement.tsd.mapping import (
    TSD_LISA1_COLUMNS,
    TSD_MAIN_COLUMNS,
    TSD_SCHEMA_REF,
    TSD_TAXONOMY_NS,
)
from saebooks.services.lodgement.tsd.serializer import (
    TsdReportingContext,
    build_tsd_lisa1_csv_document,
    build_tsd_main_csv_document,
    build_tsd_xml_document,
    persist_tsd_return,
)

__all__ = [
    "PAYMENT_TYPE_WAGES",
    "TsdDataQualityError",
    "TsdLisa1Row",
    "TsdListing",
    "TsdMainTotals",
    "generate_tsd",
    "TSD_LISA1_COLUMNS",
    "TSD_MAIN_COLUMNS",
    "TSD_SCHEMA_REF",
    "TSD_TAXONOMY_NS",
    "TsdReportingContext",
    "build_tsd_lisa1_csv_document",
    "build_tsd_main_csv_document",
    "build_tsd_xml_document",
    "persist_tsd_return",
]
