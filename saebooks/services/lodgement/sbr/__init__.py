"""SBR (Standard Business Reporting) generators — PUBLIC SHIM.

The ATO SBR XBRL document generation is part of the certified-transmission path
and is stubbed in the open engine (the community build computes the return and,
for Estonia, generates the KMD file — see ``services/lodgement/kmd/`` — but the
ATO SBR document generation raises ``NotImplementedError("commercial feature")``).

The full public symbol set is preserved so kept code
(``api/v1/tax_returns.py``) imports unchanged: the data shapes are real; the
``build_*`` / ``build_instance`` / ``envelope_parts`` functions raise.
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
