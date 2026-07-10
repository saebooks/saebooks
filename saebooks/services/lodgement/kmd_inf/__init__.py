"""KMD-INF (VAT-return invoice annex) — listing generator + file serializer.

Packet 1 (``generator.py``): the row-set generator. Packet 2
(``mapping.py``/``serializer.py``, mirroring
``services/lodgement/kmd/``): the e-MTA XML/CSV file serializer.
"""
from __future__ import annotations

from saebooks.services.lodgement.kmd_inf.generator import (
    REPORTING_TYPE_TO_KMD_BOX,
    CreditNoteAggregation,
    KmdInfDataQualityError,
    KmdInfListing,
    KmdInfPartARow,
    KmdInfPartBRow,
    generate_kmd_inf,
)
from saebooks.services.lodgement.kmd_inf.mapping import (
    KMD_INF_PART_A_COLUMNS,
    KMD_INF_PART_B_COLUMNS,
    KMD_INF_SCHEMA_REF,
    KMD_INF_TAXONOMY_NS,
)
from saebooks.services.lodgement.kmd_inf.serializer import (
    KmdInfReportingContext,
    build_kmd_inf_part_a_csv_document,
    build_kmd_inf_part_b_csv_document,
    build_kmd_inf_xml_document,
)

__all__ = [
    "REPORTING_TYPE_TO_KMD_BOX",
    "CreditNoteAggregation",
    "KmdInfDataQualityError",
    "KmdInfListing",
    "KmdInfPartARow",
    "KmdInfPartBRow",
    "generate_kmd_inf",
    "KMD_INF_PART_A_COLUMNS",
    "KMD_INF_PART_B_COLUMNS",
    "KMD_INF_SCHEMA_REF",
    "KMD_INF_TAXONOMY_NS",
    "KmdInfReportingContext",
    "build_kmd_inf_part_a_csv_document",
    "build_kmd_inf_part_b_csv_document",
    "build_kmd_inf_xml_document",
]
