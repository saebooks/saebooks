"""Parse + XSD-validate the operationAccepted / operationRejected feedback messages.

Pure tests — no DB, no network. Proves:

* our authored sample instances validate against the REAL in-tree XSDs
  (``operationaccepted.xsd`` / ``operationrejected.xsd``), mirroring
  ``test_emta_schema_validation.py``'s discipline;
* the parser extracts the fields the state machine relies on
  (vat_payable/overpaid, declaration_state, functional/xml errors);
* the dispatch + error paths behave.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from saebooks.services.lodgement.adapters.ee_messages import (
    EEMessageParseError,
    parse_feedback_message,
    parse_operation_accepted,
    parse_operation_rejected,
    validate_against,
)

_FIX = Path(__file__).parent.parent.parent / "fixtures" / "emta_schemas"


def _accepted_bytes() -> bytes:
    return (_FIX / "operation_accepted_sample.xml").read_bytes()


def _rejected_bytes() -> bytes:
    return (_FIX / "operation_rejected_sample.xml").read_bytes()


# ---- XSD validation --------------------------------------------------------


def test_accepted_sample_validates_against_real_xsd() -> None:
    validate_against(_accepted_bytes(), _FIX / "operationaccepted.xsd")


def test_rejected_sample_validates_against_real_xsd() -> None:
    validate_against(_rejected_bytes(), _FIX / "operationrejected.xsd")


# ---- accepted parsing ------------------------------------------------------


def test_parse_operation_accepted_fields() -> None:
    fb = parse_operation_accepted(_accepted_bytes())
    assert fb.accepted is True
    assert fb.request_id == "c99bbd83-28f8-48a8-ad2e-02fcad97804f"
    assert fb.vat_payable == Decimal("1234.56")
    assert fb.overpaid_vat is None
    assert fb.declaration_state == "SUBMITTED"
    assert fb.declaration_type == "1"
    assert fb.taxpayer_reg_code == "10123456"
    assert fb.year == 2027
    assert fb.month == 1
    # A functional note may ride on an accepted message.
    assert len(fb.functional_errors) == 1
    assert fb.functional_errors[0].error_pointer == "KMD_9"
    assert fb.functional_errors[0].description is not None
    assert fb.xml_errors == []


def test_parse_accepted_monetary_is_decimal_not_float() -> None:
    fb = parse_operation_accepted(_accepted_bytes())
    assert isinstance(fb.vat_payable, Decimal)


# ---- rejected parsing ------------------------------------------------------


def test_parse_operation_rejected_fields() -> None:
    fb = parse_operation_rejected(_rejected_bytes())
    assert fb.accepted is False
    assert fb.request_id == "c99bbd83-28f8-48a8-ad2e-02fcad97804f"
    assert len(fb.xml_errors) == 1
    assert "not expected" in fb.xml_errors[0]
    assert len(fb.functional_errors) == 1
    assert fb.functional_errors[0].error_pointer == "KMD_4"
    assert fb.functional_errors[0].original_value == "-5.00"
    assert fb.vat_payable is None


# ---- dispatch + error paths ------------------------------------------------


def test_dispatch_routes_by_root() -> None:
    assert parse_feedback_message(_accepted_bytes()).accepted is True
    assert parse_feedback_message(_rejected_bytes()).accepted is False


def test_dispatch_unknown_root_raises() -> None:
    with pytest.raises(EEMessageParseError, match="unrecognised"):
        parse_feedback_message(b"<somethingElse><requestId>x</requestId></somethingElse>")


def test_accepted_parser_rejects_wrong_root() -> None:
    with pytest.raises(EEMessageParseError, match="expected <operationAccepted>"):
        parse_operation_accepted(_rejected_bytes())


def test_missing_request_id_raises() -> None:
    with pytest.raises(EEMessageParseError, match="requestId"):
        parse_operation_accepted(
            b"<operationAccepted><declarationState>X</declarationState></operationAccepted>"
        )


def test_malformed_xml_raises() -> None:
    with pytest.raises(EEMessageParseError):
        parse_feedback_message(b"<not-closed")
