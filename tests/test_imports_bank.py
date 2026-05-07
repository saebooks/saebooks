"""Pure parser tests for bank CSV + OFX importers.

Each AU bank has its own quirks — these tests pin the per-format
parser against representative fixture CSVs that match what the
bank's "export transactions" feature actually emits.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from saebooks.services.imports import bank_csv, bank_ofx
from saebooks.services.imports.bank_csv import BankCsvError, BankCsvFormat


def test_detect_cba() -> None:
    raw = "15/04/2026,-42.50,Coles Supermarkets,1234.00"
    assert bank_csv.detect_format(raw) == BankCsvFormat.CBA


def test_detect_anz() -> None:
    raw = "15/04/2026,-42.50,Coles Supermarkets"
    assert bank_csv.detect_format(raw) == BankCsvFormat.ANZ


def test_detect_nab() -> None:
    raw = "15/04/2026,-42.50,,,Coles Supermarkets,1234.00"
    assert bank_csv.detect_format(raw) == BankCsvFormat.NAB


def test_detect_westpac() -> None:
    raw = (
        '"Date","Narrative","Debit Amount","Credit Amount","Balance","Categories","Serial"\n'
        '"15/04/2026","Coles Supermarkets","42.50","","1234.00","",""'
    )
    assert bank_csv.detect_format(raw) == BankCsvFormat.WESTPAC


def test_detect_empty_raises() -> None:
    with pytest.raises(BankCsvError):
        bank_csv.detect_format("")


def test_parse_cba_one_row() -> None:
    raw = "15/04/2026,-42.50,Coles Supermarkets,1234.00"
    lines = bank_csv.parse_bank_csv(raw)
    assert len(lines) == 1
    assert lines[0].amount == Decimal("-42.50")
    assert lines[0].description == "Coles Supermarkets"
    assert lines[0].txn_date.isoformat() == "2026-04-15"


def test_parse_anz_multi_row() -> None:
    raw = (
        "15/04/2026,-42.50,Coles Supermarkets\n"
        "16/04/2026,1000.00,Salary\n"
    )
    lines = bank_csv.parse_bank_csv(raw)
    assert len(lines) == 2
    assert lines[1].amount == Decimal("1000.00")
    assert lines[1].description == "Salary"


def test_parse_nab() -> None:
    raw = (
        "15/04/2026,-42.50,,,Coles Supermarkets,1234.00\n"
        "16/04/2026,1000.00,,,Salary,2234.00"
    )
    lines = bank_csv.parse_bank_csv(raw)
    assert len(lines) == 2
    assert lines[0].description == "Coles Supermarkets"
    assert lines[1].amount == Decimal("1000.00")


def test_parse_westpac_debit_credit_split() -> None:
    raw = (
        '"Date","Narrative","Debit Amount","Credit Amount","Balance","Categories","Serial"\n'
        '"15/04/2026","Coles Supermarkets","42.50","","1234.00","",""\n'
        '"16/04/2026","Salary","","1000.00","2234.00","",""'
    )
    lines = bank_csv.parse_bank_csv(raw)
    assert len(lines) == 2
    # Debit = withdrawal = negative in our model.
    assert lines[0].amount == Decimal("-42.50")
    # Credit = deposit = positive.
    assert lines[1].amount == Decimal("1000.00")


def test_generic_strips_currency_and_commas() -> None:
    raw = (
        'Date,Description,Amount\n'
        '15/04/2026,Coles,"-1,234.56"\n'
        '16/04/2026,Salary,$10000\n'
    )
    lines = bank_csv.parse_bank_csv(raw)
    assert lines[0].amount == Decimal("-1234.56")
    assert lines[1].amount == Decimal("10000")


def test_generic_alt_date_formats() -> None:
    raw = "Date,Description,Amount\n2026-04-15,Coles,-42.50\n"
    lines = bank_csv.parse_bank_csv(raw)
    assert lines[0].txn_date.isoformat() == "2026-04-15"


def test_generic_missing_amount_raises() -> None:
    raw = "Date,Description\n15/04/2026,Coles\n"
    with pytest.raises(BankCsvError):
        bank_csv.parse_bank_csv(raw)


# --- OFX ------------------------------------------------------------


OFX1_FIXTURE = """\
OFXHEADER:100
DATA:OFXSGML

<OFX>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260415
<TRNAMT>-42.50
<FITID>TXN00001
<NAME>Coles Supermarkets
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260416
<TRNAMT>1000.00
<FITID>TXN00002
<NAME>Salary
</STMTTRN>
</BANKTRANLIST>
</OFX>
"""

OFX2_FIXTURE = """\
<?xml version="1.0" encoding="utf-8"?>
<?OFX OFXHEADER="200" VERSION="200"?>
<OFX>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <STMTRS>
        <BANKTRANLIST>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20260415</DTPOSTED>
            <TRNAMT>-42.50</TRNAMT>
            <FITID>TXN00001</FITID>
            <NAME>Coles Supermarkets</NAME>
          </STMTTRN>
        </BANKTRANLIST>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
"""


def test_parse_ofx1() -> None:
    lines = bank_ofx.parse_ofx(OFX1_FIXTURE)
    assert len(lines) == 2
    assert lines[0].txn_date.isoformat() == "2026-04-15"
    assert lines[0].amount == Decimal("-42.50")
    assert lines[0].description == "Coles Supermarkets"
    assert lines[0].reference == "TXN00001"


def test_parse_ofx2() -> None:
    lines = bank_ofx.parse_ofx(OFX2_FIXTURE)
    assert len(lines) == 1
    assert lines[0].reference == "TXN00001"
    assert lines[0].amount == Decimal("-42.50")


def test_parse_ofx_empty_raises() -> None:
    with pytest.raises(bank_ofx.OfxError):
        bank_ofx.parse_ofx("")


def test_parse_ofx_bytes() -> None:
    """Byte input is transparently decoded."""
    lines = bank_ofx.parse_ofx(OFX1_FIXTURE.encode("utf-8"))
    assert len(lines) == 2
