"""STP2 PAYEVNT SBR document generator — PUBLIC SHIM (generation stubbed).

The STP2 XBRL PAYEVNT generation is part of the certified ATO transmission path
and is NOT shipped in the open engine. The public symbol
``build_stp_pay_event_document`` is preserved (kept code imports it from the
``sbr`` package) but raises ``NotImplementedError("commercial feature")``.
"""
from __future__ import annotations

from typing import Any

_COMMERCIAL = (
    "commercial feature: ATO SBR STP2 PAYEVNT document generation is not "
    "available in the open engine"
)


def build_stp_pay_event_document(payload: dict[str, Any]) -> bytes:
    raise NotImplementedError(_COMMERCIAL)
