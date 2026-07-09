"""SBR (Standard Business Reporting) business-document generators.

Engine-side XBRL generation for ATO lodgement. The engine produces the
business document; the private commercial lodge-server signs it (ATO Machine
Credential) and handles ebMS3/AS4 transport — that server and its API
contract are private/commercial, not part of this repository.

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
    "Fact",
    "ReportingContext",
    "XbrlInstance",
    "build_bas_document",
    "build_instance",
    "build_stp_pay_event_document",
    "envelope_parts",
]
