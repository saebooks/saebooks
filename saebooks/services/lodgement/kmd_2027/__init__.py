"""2027 data-based KMD exporter (XBRL GL, section EE0203001).

READY FOR the data-based KMD the day it becomes law — NOT "compliant with" it:
the 2027 mandate is at VTK / consultation stage, not enacted (build-plan §4.5).

Pieces (build-plan §4.2), reusing the shipped KMD-INF listing spine + the box
engine, NOT rebuilding them:

* ``generator.generate_kmd_2027`` — the KMD-INF listing generator with the
  €1,000 threshold OFF and a ``KMDTYYP2026ap`` code ON per transaction row
  (DB-bound; postgres_only).
* ``serializer.build_kmd_2027_xml_document`` — the XBRL GL COR+BUS+EXT exporter,
  golden-filed against the official package sample (pure; no DB).
* ``kmdtyyp`` — the ``KMDTYYP2026ap`` classification loader + engine↔leaf map
  (pure; loads ``seeds/jurisdictions/EE/kmdtyyp_mapping.yaml`` off disk).
* ``reconcile`` — local koondvaade reconciliation: recompute the KMD box vector
  from the same posted ledger via the box engine and check the exported rows
  sum, by category, to it (pure core + postgres_only DB wrapper).

Transmission is NOT here — it is Module 3's X-Road KMD3 rail. This module only
PRODUCES the payload; filing it waits on live mTLS creds (and the law).
"""
from __future__ import annotations

from saebooks.services.lodgement.kmd_2027.serializer import (
    Kmd2027DataQualityError,
    Kmd2027Listing,
    Kmd2027ReportingContext,
    Kmd2027Row,
    build_kmd_2027_xml_document,
)

__all__ = [
    "Kmd2027DataQualityError",
    "Kmd2027Listing",
    "Kmd2027ReportingContext",
    "Kmd2027Row",
    "build_kmd_2027_xml_document",
]
