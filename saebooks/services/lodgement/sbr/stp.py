"""STP2 PAYEVNT (Single Touch Payroll Phase 2) SBR document generator —
COMMUNITY EDITION STUB.

The community (AGPL) edition does not ship the regulator-facing STP2
PAYEVNT XBRL document generator (employer + per-payee PAYEVNTEMP records
against the ATO STP2 MIG taxonomy). Building + validating that transmission
document is a commercial SAE Books feature — see CHARTER.md / LICENSING.md.
``build_stp_pay_event_document`` raises ``NotImplementedError`` in this
edition.
"""
from __future__ import annotations

from typing import Any


def build_stp_pay_event_document(payload: dict[str, Any]) -> bytes:
    """Render an STP2 PAYEVNT as XBRL instance bytes from a build_pay_event payload.

    COMMUNITY EDITION STUB — always raises. ``payload`` would be the dict
    produced by ``services/stp.py:build_pay_event`` (also persisted as
    ``StpSubmission.payload``); the community edition computes that payload
    (AGPL) but does not ship the ATO PAYEVNT.0004 transmission-document
    generator.
    """
    raise NotImplementedError(
        "Certified e-lodgement is a commercial SAE Books feature; the community "
        "edition ships box definitions + the return calculator but not the "
        "regulator transmission adapters. See CHARTER.md / LICENSING.md."
    )
