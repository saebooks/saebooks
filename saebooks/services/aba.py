"""ABA / CEMTEX Direct Entry bank-file builder.

Generates a byte-exact ABA file in the APCA CEMTEX format that CBA,
ANZ, NAB, Westpac, Bendigo, Macquarie, and every other AU bank that
still honours the 1970s Batch Payments product will ingest.

Format primer (every record is 120 chars, CRLF-terminated):

**Type 0 — Header (one per file)**

    Pos  1    '0'
    Pos  2-18  17 blank
    Pos 19-20  Reel sequence '01'
    Pos 21-23  Bank abbreviation ('CBA' etc)
    Pos 24-30   7 blank
    Pos 31-56  User name (26 chars, space-padded)
    Pos 57-62  APCA User ID (6 digits)
    Pos 63-74  Description (12 chars, space-padded)
    Pos 75-80  Date to be processed (DDMMYY)
    Pos 81-120 40 blank

**Type 1 — Detail (one per payment line)**

    Pos  1    '1'
    Pos  2-8   Payee BSB 'xxx-xxx'
    Pos  9-17  Payee account (9 chars, right-justified, space-padded)
    Pos 18    Indicator ' ' / 'N' / 'W' / 'X' / 'Y'
    Pos 19-20  Transaction code '50' (credit) / '13' (debit)
    Pos 21-30  Amount in cents, 10 digits, zero-padded left
    Pos 31-62  Account title (32 chars, space-padded)
    Pos 63-80  Lodgement reference (18 chars, space-padded)
    Pos 81-87  Remitter BSB 'xxx-xxx'
    Pos 88-96  Remitter account (9 chars, right-justified, space-padded)
    Pos 97-112 Remitter name (16 chars, space-padded)
    Pos 113-120 Withholding tax amount (8 digits cents, zero-padded)

**Type 7 — Trailer (one per file)**

    Pos  1    '7'
    Pos  2-8   BSB filler '999-999'
    Pos  9-20  12 blank
    Pos 21-30  Net total amount (= abs(credit_total - debit_total))
    Pos 31-40  Credit total
    Pos 41-50  Debit total
    Pos 51-74  24 blank
    Pos 75-80  Total item count (6 digits)
    Pos 81-120 40 blank

For a standard outgoing pay-run, every detail line uses transaction
code ``50`` (credit) and the trailer's net/credit totals match and
the debit total is 00000000000. The "self-balancing" variant — where
a matching type-1 debit for the sum is appended — is NOT emitted;
most banks tolerate either and the non-balancing form is simpler.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Every line in an ABA file is CRLF-terminated. Strictly, APCA only
# requires a LF between records but every bank parser we've seen
# accepts CRLF and some (Westpac's legacy ingest) require it.
_LINE_ENDING = "\r\n"

# ABA credit/debit transaction codes. For accounts-payable pay runs
# the dominant code is 50 ("General credit"); 13 is a direct debit
# ("External debit"). Exposed so payroll and ATO/super runs can use
# 50 (super contributions), 40 (MGM payroll), etc.
TXN_CREDIT_GENERAL = "50"
TXN_DEBIT_EXTERNAL = "13"


class AbaError(ValueError):
    """Raised on any ABA field-validation failure.

    Field rules come from the APCA ``Direct Entry User Specifications``
    (CSIRO, rev 2010). The validation here is deliberately strict — a
    single invalid character anywhere in a 120-char record will cause
    the sponsor bank to reject the entire file. Failing loudly at the
    Python boundary is cheaper than getting a 24-hour-delayed NACK
    from the bank.
    """


@dataclass(frozen=True)
class AbaHeader:
    bank_abbreviation: str    # 'CBA', 'ANZ', 'NAB', 'WBC', 'BOQ', etc.
    user_name: str            # Our trading name as known to the bank
    apca_user_id: str         # 6-digit Direct Entry User ID
    description: str          # 'PAYROLL', 'CREDITORS', etc. (<=12 chars)
    process_date_ddmmyy: str  # e.g. '210426' for 21-Apr-2026


@dataclass(frozen=True)
class AbaDetail:
    payee_bsb: str               # 'xxx-xxx'
    payee_account_number: str    # up to 9 chars
    payee_account_title: str     # up to 32 chars
    amount_cents: int            # positive integer; sign comes from txn_code
    lodgement_reference: str     # up to 18 chars (shows on payee statement)
    remitter_bsb: str            # our BSB 'xxx-xxx'
    remitter_account_number: str # our account number
    remitter_name: str           # up to 16 chars (shows on payee statement)
    txn_code: str = TXN_CREDIT_GENERAL  # '50' credit / '13' debit / …
    withholding_tax_cents: int = 0
    indicator: str = " "         # ' ' / 'N' / 'W' / 'X' / 'Y'


# ---------------------------------------------------------------------- #
# Field helpers                                                           #
# ---------------------------------------------------------------------- #


_BSB_RE = re.compile(r"^\d{3}-\d{3}$")
# ABA printable charset: A-Z, a-z, 0-9, space, and a handful of symbols.
# Strict per APCA spec; banks reject the rest.
_ABA_PRINTABLE_RE = re.compile(r"^[A-Za-z0-9 \-&/.',()+*#@]*$")


def _pad_right(value: str, width: int, *, field: str) -> str:
    if len(value) > width:
        raise AbaError(f"{field}: value {value!r} exceeds {width} chars")
    if not _ABA_PRINTABLE_RE.match(value):
        raise AbaError(
            f"{field}: value {value!r} contains non-ABA-printable characters"
        )
    return value.ljust(width, " ")


def _pad_left_digits(value: int, width: int, *, field: str) -> str:
    if value < 0:
        raise AbaError(f"{field}: negative value {value} not allowed")
    out = str(value)
    if len(out) > width:
        raise AbaError(f"{field}: value {value} exceeds {width} digits")
    return out.rjust(width, "0")


def _validate_bsb(bsb: str, *, field: str) -> str:
    if not _BSB_RE.match(bsb):
        raise AbaError(
            f"{field}: BSB {bsb!r} must be formatted as 'xxx-xxx' (got "
            f"{len(bsb)} chars)"
        )
    return bsb


def _validate_account_number(number: str, *, field: str) -> str:
    # ABA allows up to 9 chars; most AU account numbers are 6-9 digits
    # but the spec permits hyphens and spaces (e.g. '1234 5678').
    if not number or len(number) > 9:
        raise AbaError(
            f"{field}: account number {number!r} must be 1..9 chars"
        )
    if not _ABA_PRINTABLE_RE.match(number):
        raise AbaError(
            f"{field}: account number {number!r} contains non-ABA chars"
        )
    # Right-justify, space-pad to 9.
    return number.rjust(9, " ")


def _validate_bank_abbreviation(abbr: str) -> str:
    if len(abbr) != 3 or not abbr.isalpha() or not abbr.isupper():
        raise AbaError(
            f"bank_abbreviation: {abbr!r} must be exactly 3 upper-case "
            "letters (e.g. 'CBA')"
        )
    return abbr


def _validate_apca_id(user_id: str) -> str:
    if len(user_id) != 6 or not user_id.isdigit():
        raise AbaError(
            f"apca_user_id: {user_id!r} must be exactly 6 digits"
        )
    return user_id


def _validate_ddmmyy(ddmmyy: str) -> str:
    if len(ddmmyy) != 6 or not ddmmyy.isdigit():
        raise AbaError(
            f"process_date_ddmmyy: {ddmmyy!r} must be 6 digits as DDMMYY"
        )
    return ddmmyy


def _validate_txn_code(code: str) -> str:
    # Allowed codes per APCA: 13, 50, 51, 52, 53, 54, 55, 56, 57.
    # We accept the common ones; anything else raises.
    if code not in {"13", "50", "51", "52", "53", "54", "55", "56", "57"}:
        raise AbaError(f"txn_code: {code!r} is not a recognised APCA code")
    return code


def _validate_indicator(ind: str) -> str:
    if ind not in {" ", "N", "W", "X", "Y"}:
        raise AbaError(
            f"indicator: {ind!r} must be ' ', 'N', 'W', 'X' or 'Y'"
        )
    return ind


# ---------------------------------------------------------------------- #
# Record builders                                                         #
# ---------------------------------------------------------------------- #


def _build_header(h: AbaHeader) -> str:
    line = (
        "0"                                                          # 1
        + " " * 17                                                   # 2-18
        + "01"                                                       # 19-20
        + _validate_bank_abbreviation(h.bank_abbreviation)           # 21-23
        + " " * 7                                                    # 24-30
        + _pad_right(h.user_name, 26, field="user_name")             # 31-56
        + _validate_apca_id(h.apca_user_id)                          # 57-62
        + _pad_right(h.description, 12, field="description")         # 63-74
        + _validate_ddmmyy(h.process_date_ddmmyy)                    # 75-80
        + " " * 40                                                   # 81-120
    )
    assert len(line) == 120, f"header is {len(line)} chars"
    return line


def _build_detail(d: AbaDetail) -> str:
    line = (
        "1"                                                               # 1
        + _validate_bsb(d.payee_bsb, field="payee_bsb")                   # 2-8
        + _validate_account_number(                                        # 9-17
            d.payee_account_number, field="payee_account_number"
        )
        + _validate_indicator(d.indicator)                                # 18
        + _validate_txn_code(d.txn_code)                                  # 19-20
        + _pad_left_digits(d.amount_cents, 10, field="amount_cents")     # 21-30
        + _pad_right(                                                     # 31-62
            d.payee_account_title, 32, field="payee_account_title"
        )
        + _pad_right(                                                     # 63-80
            d.lodgement_reference, 18, field="lodgement_reference"
        )
        + _validate_bsb(d.remitter_bsb, field="remitter_bsb")             # 81-87
        + _validate_account_number(                                        # 88-96
            d.remitter_account_number, field="remitter_account_number"
        )
        + _pad_right(d.remitter_name, 16, field="remitter_name")         # 97-112
        + _pad_left_digits(                                               # 113-120
            d.withholding_tax_cents, 8, field="withholding_tax_cents"
        )
    )
    assert len(line) == 120, f"detail is {len(line)} chars"
    return line


def _build_trailer(details: list[AbaDetail]) -> str:
    credit_total = sum(
        d.amount_cents for d in details if d.txn_code in {"50", "51", "52", "53"}
    )
    debit_total = sum(
        d.amount_cents for d in details if d.txn_code == "13"
    )
    net_total = abs(credit_total - debit_total)

    line = (
        "7"                                                              # 1
        + "999-999"                                                      # 2-8
        + " " * 12                                                       # 9-20
        + _pad_left_digits(net_total, 10, field="net_total")             # 21-30
        + _pad_left_digits(credit_total, 10, field="credit_total")       # 31-40
        + _pad_left_digits(debit_total, 10, field="debit_total")         # 41-50
        + " " * 24                                                       # 51-74
        + _pad_left_digits(len(details), 6, field="item_count")          # 75-80
        + " " * 40                                                       # 81-120
    )
    assert len(line) == 120, f"trailer is {len(line)} chars"
    return line


def build_aba(header: AbaHeader, details: list[AbaDetail]) -> str:
    """Render a full ABA file as a CRLF-joined string.

    At least one detail line is required — an empty file is a
    validation error; banks reject it too.
    """
    if not details:
        raise AbaError("ABA file requires at least one detail record")
    out = [_build_header(header)]
    out.extend(_build_detail(d) for d in details)
    out.append(_build_trailer(details))
    # Trailing CRLF is idiomatic — some banks' parsers treat a
    # bare-LF-ended final record as truncated.
    return _LINE_ENDING.join(out) + _LINE_ENDING


# ---------------------------------------------------------------------- #
# Decimal-to-cents helper                                                 #
# ---------------------------------------------------------------------- #


def dollars_to_cents(amount: Decimal) -> int:
    """Convert a Decimal dollar amount to an integer number of cents.

    Rounds half-up — matches the rest of the app's money handling.
    Negative amounts raise, matching the ABA spec (sign is encoded
    in the txn_code, not the amount).
    """
    if amount < 0:
        raise AbaError(f"amount {amount} must be non-negative for ABA")
    cents = (amount * Decimal("100")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(cents)
