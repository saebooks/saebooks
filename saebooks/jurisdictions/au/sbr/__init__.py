"""AU SBR (Standard Business Reporting) document generators.

ATO lodgement business-document generation that is AU jurisdiction
compute, relocated here from ``services/lodgement/sbr`` per the
neutral-core rule (core imports zero jurisdiction modules; the AU
bolt-on module owns AU reference data + compute).

* ``tpar`` — TPAR.0003 (Taxable Payments Annual Report) document
  generator, pinned to the ATO conformance suite. The TPAR *aggregator*
  (walks paid bills/expenses into ``tpar_lines``) is the sibling
  ``jurisdictions/au/tpar.py``; this package builds the lodgeable
  document from those figures.

The remaining generators (``bas``/AS.0004, ``stp``/PAYEVNT.0004,
``xbrl``) still live in ``services/lodgement/sbr`` and move here in a
later phase of the AU-lodgement relocation.
"""
from __future__ import annotations

from saebooks.jurisdictions.au.sbr.tpar import (
    TparAddress,
    TparDocumentError,
    TparDocuments,
    TparIntermediary,
    TparPayeeRecord,
    TparPhone,
    TparReportingParty,
    build_tpar_document,
    build_tpar_payee_part,
    build_tpar_report_part,
)

__all__ = [
    "TparAddress",
    "TparDocumentError",
    "TparDocuments",
    "TparIntermediary",
    "TparPayeeRecord",
    "TparPhone",
    "TparReportingParty",
    "build_tpar_document",
    "build_tpar_payee_part",
    "build_tpar_report_part",
]
