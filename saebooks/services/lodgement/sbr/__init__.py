"""SBR (Standard Business Reporting) business-document generators.

Engine-side XBRL generation for ATO lodgement. The engine produces the
business document; the private lodge-server signs it (ATO Machine Credential)
and handles ebMS3/AS4 transport — see ``docs/contracts/lodge-server.md``.

⚠ The taxonomy concept names / schemaRefs are PLACEHOLDERS pending the ATO SBR
MIGs (DSP-gated) + EVTE validation. See ``xbrl`` and ``bas`` module docstrings.
"""
from __future__ import annotations

from saebooks.services.lodgement.sbr.bas import (
    BasFigures,
    build_bas_document,
)
from saebooks.services.lodgement.sbr.stp import (
    build_stp_pay_event_document,
)
from saebooks.services.lodgement.sbr.xbrl import (
    Fact,
    ReportingContext,
    XbrlInstance,
    build_instance,
    envelope_parts,
)

__all__ = [
    "BasFigures",
    "build_bas_document",
    "build_stp_pay_event_document",
    "Fact",
    "ReportingContext",
    "XbrlInstance",
    "build_instance",
    "envelope_parts",
]
