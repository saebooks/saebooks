"""Tests for ``saebooks.services.aba``.

The ABA/CEMTEX format is 55 years old and every bank's parser is
subtly different, so these tests are obsessively byte-exact. Field
positions in the format spec are 1-indexed; Python slices are
0-indexed. The header/detail/trailer builders assert their output
is exactly 120 chars wide.

Every test here runs with no DB; ``services.aba`` is pure.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from saebooks.services import aba

# ---------------------------------------------------------------------- #
# Field helpers                                                           #
# ---------------------------------------------------------------------- #


def test_dollars_to_cents_rounds_half_up() -> None:
    assert aba.dollars_to_cents(Decimal("0.00")) == 0
    assert aba.dollars_to_cents(Decimal("1.00")) == 100
    assert aba.dollars_to_cents(Decimal("1.50")) == 150
    assert aba.dollars_to_cents(Decimal("123.45")) == 12345
    # Half-up on the cents column
    assert aba.dollars_to_cents(Decimal("0.005")) == 1


def test_dollars_to_cents_rejects_negative() -> None:
    with pytest.raises(aba.AbaError, match="non-negative"):
        aba.dollars_to_cents(Decimal("-1.00"))


# ---------------------------------------------------------------------- #
# Builder round-trips                                                     #
# ---------------------------------------------------------------------- #


def _good_header() -> aba.AbaHeader:
    return aba.AbaHeader(
        bank_abbreviation="CBA",
        user_name="SAE ENGINEERING",
        apca_user_id="301500",
        description="CREDITORS",
        process_date_ddmmyy="210426",
    )


def _good_detail(amount_cents: int = 12345, lodgement: str = "SUP-0001") -> aba.AbaDetail:
    return aba.AbaDetail(
        payee_bsb="062-001",
        payee_account_number="12345678",
        payee_account_title="ACME WIDGETS PTY LTD",
        amount_cents=amount_cents,
        lodgement_reference=lodgement,
        remitter_bsb="062-000",
        remitter_account_number="11112222",
        remitter_name="SAE ENGINEERING",
    )


def test_full_file_structure() -> None:
    text = aba.build_aba(_good_header(), [_good_detail()])
    lines = text.rstrip("\r\n").split("\r\n")
    assert len(lines) == 3  # header + 1 detail + trailer
    assert all(len(ln) == 120 for ln in lines), (
        [len(ln) for ln in lines]
    )
    assert lines[0][0] == "0"
    assert lines[1][0] == "1"
    assert lines[-1][0] == "7"
    # Final CRLF present
    assert text.endswith("\r\n")


def test_header_field_positions() -> None:
    text = aba.build_aba(_good_header(), [_good_detail()])
    header = text.split("\r\n")[0]
    assert header[0] == "0"
    assert header[1:18] == " " * 17
    assert header[18:20] == "01"               # reel seq
    assert header[20:23] == "CBA"              # bank abbr
    assert header[23:30] == " " * 7
    assert header[30:56] == "SAE ENGINEERING".ljust(26)
    assert header[56:62] == "301500"
    assert header[62:74] == "CREDITORS".ljust(12)
    assert header[74:80] == "210426"
    assert header[80:120] == " " * 40


def test_detail_field_positions() -> None:
    text = aba.build_aba(_good_header(), [_good_detail()])
    detail = text.split("\r\n")[1]
    assert detail[0] == "1"
    assert detail[1:8] == "062-001"
    assert detail[8:17] == "12345678".rjust(9)
    assert detail[17] == " "                    # indicator
    assert detail[18:20] == "50"                # txn code (credit)
    assert detail[20:30] == "0000012345"        # cents, zero-padded
    assert detail[30:62] == "ACME WIDGETS PTY LTD".ljust(32)
    assert detail[62:80] == "SUP-0001".ljust(18)
    assert detail[80:87] == "062-000"
    assert detail[87:96] == "11112222".rjust(9)
    assert detail[96:112] == "SAE ENGINEERING".ljust(16)
    assert detail[112:120] == "00000000"        # withholding tax


def test_trailer_totals_single_credit() -> None:
    """One $123.45 credit: net = credit = 12345 cents, debit = 0, count = 1."""
    text = aba.build_aba(_good_header(), [_good_detail(amount_cents=12345)])
    trailer = text.split("\r\n")[2]
    assert trailer[0] == "7"
    assert trailer[1:8] == "999-999"
    assert trailer[8:20] == " " * 12
    assert trailer[20:30] == "0000012345"  # net
    assert trailer[30:40] == "0000012345"  # credit
    assert trailer[40:50] == "0000000000"  # debit
    assert trailer[50:74] == " " * 24
    assert trailer[74:80] == "000001"      # item count
    assert trailer[80:120] == " " * 40


def test_trailer_multi_line_sum() -> None:
    details = [
        _good_detail(amount_cents=10_000),
        _good_detail(amount_cents=25_000),
        _good_detail(amount_cents=500),
    ]
    text = aba.build_aba(_good_header(), details)
    trailer = text.rstrip("\r\n").split("\r\n")[-1]
    # Totals: 100 + 250 + 5 = $355.00 = 35500 cents
    assert trailer[20:30] == "0000035500"
    assert trailer[30:40] == "0000035500"
    assert trailer[40:50] == "0000000000"
    assert trailer[74:80] == "000003"


def test_net_total_is_abs_credit_minus_debit() -> None:
    credit = aba.AbaDetail(
        payee_bsb="062-001",
        payee_account_number="12345678",
        payee_account_title="CREDIT PAYEE",
        amount_cents=30_000,
        lodgement_reference="CR",
        remitter_bsb="062-000",
        remitter_account_number="11112222",
        remitter_name="US",
        txn_code=aba.TXN_CREDIT_GENERAL,
    )
    debit = aba.AbaDetail(
        payee_bsb="062-001",
        payee_account_number="12345678",
        payee_account_title="DEBIT PAYEE",
        amount_cents=10_000,
        lodgement_reference="DR",
        remitter_bsb="062-000",
        remitter_account_number="11112222",
        remitter_name="US",
        txn_code=aba.TXN_DEBIT_EXTERNAL,
    )
    text = aba.build_aba(_good_header(), [credit, debit])
    trailer = text.rstrip("\r\n").split("\r\n")[-1]
    # net = |30000 - 10000| = 20000
    assert trailer[20:30] == "0000020000"
    assert trailer[30:40] == "0000030000"  # credit
    assert trailer[40:50] == "0000010000"  # debit
    assert trailer[74:80] == "000002"


# ---------------------------------------------------------------------- #
# Validation                                                              #
# ---------------------------------------------------------------------- #


def test_empty_details_rejected() -> None:
    with pytest.raises(aba.AbaError, match="at least one"):
        aba.build_aba(_good_header(), [])


def test_bad_bsb_format_rejected() -> None:
    with pytest.raises(aba.AbaError, match="BSB"):
        detail = aba.AbaDetail(
            payee_bsb="062001",  # missing hyphen
            payee_account_number="12345678",
            payee_account_title="X",
            amount_cents=1,
            lodgement_reference="R",
            remitter_bsb="062-000",
            remitter_account_number="11112222",
            remitter_name="US",
        )
        aba.build_aba(_good_header(), [detail])


def test_account_number_too_long_rejected() -> None:
    with pytest.raises(aba.AbaError, match="account number"):
        detail = aba.AbaDetail(
            payee_bsb="062-001",
            payee_account_number="1234567890",  # 10 chars > 9
            payee_account_title="X",
            amount_cents=1,
            lodgement_reference="R",
            remitter_bsb="062-000",
            remitter_account_number="11112222",
            remitter_name="US",
        )
        aba.build_aba(_good_header(), [detail])


def test_bank_abbreviation_must_be_3_upper() -> None:
    with pytest.raises(aba.AbaError, match="bank_abbreviation"):
        aba.build_aba(
            aba.AbaHeader(
                bank_abbreviation="cba",  # lowercase
                user_name="X",
                apca_user_id="301500",
                description="Y",
                process_date_ddmmyy="210426",
            ),
            [_good_detail()],
        )


def test_apca_user_id_must_be_6_digits() -> None:
    with pytest.raises(aba.AbaError, match="apca_user_id"):
        aba.build_aba(
            aba.AbaHeader(
                bank_abbreviation="CBA",
                user_name="X",
                apca_user_id="30150",  # 5 digits
                description="Y",
                process_date_ddmmyy="210426",
            ),
            [_good_detail()],
        )


def test_ddmmyy_format_enforced() -> None:
    with pytest.raises(aba.AbaError, match="process_date_ddmmyy"):
        aba.build_aba(
            aba.AbaHeader(
                bank_abbreviation="CBA",
                user_name="X",
                apca_user_id="301500",
                description="Y",
                process_date_ddmmyy="2026-04-21",  # iso date
            ),
            [_good_detail()],
        )


def test_amount_exceeding_10_digits_rejected() -> None:
    with pytest.raises(aba.AbaError, match="amount_cents"):
        detail = aba.AbaDetail(
            payee_bsb="062-001",
            payee_account_number="12345678",
            payee_account_title="X",
            amount_cents=10_000_000_000,  # 11 digits
            lodgement_reference="R",
            remitter_bsb="062-000",
            remitter_account_number="11112222",
            remitter_name="US",
        )
        aba.build_aba(_good_header(), [detail])


def test_non_printable_char_rejected() -> None:
    """ABA restricts the charset to a specific printable set; any
    rogue character (tab, non-ASCII, curly-quote) has to be rejected
    before it gets to the bank."""
    with pytest.raises(aba.AbaError, match="non-ABA"):
        detail = aba.AbaDetail(
            payee_bsb="062-001",
            payee_account_number="12345678",
            payee_account_title="ACME – WIDGETS",  # en-dash
            amount_cents=1,
            lodgement_reference="R",
            remitter_bsb="062-000",
            remitter_account_number="11112222",
            remitter_name="US",
        )
        aba.build_aba(_good_header(), [detail])


def test_field_too_long_rejected() -> None:
    with pytest.raises(aba.AbaError, match="user_name"):
        aba.build_aba(
            aba.AbaHeader(
                bank_abbreviation="CBA",
                user_name="X" * 27,  # 27 > 26
                apca_user_id="301500",
                description="Y",
                process_date_ddmmyy="210426",
            ),
            [_good_detail()],
        )


def test_txn_code_must_be_valid() -> None:
    with pytest.raises(aba.AbaError, match="txn_code"):
        detail = aba.AbaDetail(
            payee_bsb="062-001",
            payee_account_number="12345678",
            payee_account_title="X",
            amount_cents=1,
            lodgement_reference="R",
            remitter_bsb="062-000",
            remitter_account_number="11112222",
            remitter_name="US",
            txn_code="99",  # not in APCA list
        )
        aba.build_aba(_good_header(), [detail])
