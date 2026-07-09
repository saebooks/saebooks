"""AU bank CSV importer.

Parses the downloadable CSV formats of the big-four Australian banks
into a uniform ``ParsedLine`` shape so the caller can feed them into
``BankStatementLine`` rows.

Format detection runs off the header row (case-insensitive substring
match) so we don't depend on exact column order — banks tweak their
templates every couple of years.

Known formats (as of 2026-04):

* ``cba``  — Commonwealth Bank. No header row. Columns:
    Date, Amount, Description, Balance
* ``anz``  — ANZ. Header-less. Columns:
    Date, Amount, Description (balance not exported)
* ``nab``  — NAB. Header-less. Columns:
    Date, Amount, "" (category), "" (merchant), Description, Balance
* ``westpac`` — Westpac. Has a header row.
    "Date","Narrative","Debit Amount","Credit Amount","Balance","Categories","Serial"
* ``generic`` — Fallback. Needs a header with "date", "amount",
  "description" (or "narration" / "narrative"). Debit/credit columns
  are combined into a signed ``amount`` (withdrawal = negative).

Idempotency: caller hashes ``(account_id, date, amount, description)``
into a stable ``external_id`` so re-importing the same CSV doesn't
double up. ``insert_statement_lines``-style ON CONFLICT DO NOTHING in
``persist.py`` handles dedup at the DB boundary.
"""
from __future__ import annotations

import csv
import enum
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Columns we accept in "generic" mode, case-insensitive. The first
# member of each tuple is the canonical key.
_GENERIC_DATE_COLS = ("date", "transaction date", "txn date", "posted")
_GENERIC_DESC_COLS = ("description", "narrative", "narration", "details", "memo")
_GENERIC_AMOUNT_COLS = ("amount",)
_GENERIC_DEBIT_COLS = ("debit amount", "debit", "withdrawal")
_GENERIC_CREDIT_COLS = ("credit amount", "credit", "deposit")


class BankCsvFormat(enum.StrEnum):
    CBA = "cba"
    ANZ = "anz"
    NAB = "nab"
    WESTPAC = "westpac"
    GENERIC = "generic"


@dataclass(frozen=True)
class ParsedLine:
    """One bank statement line ready for persistence.

    ``amount`` is signed (positive = deposit, negative = withdrawal),
    matching ``BankStatementLine.amount`` semantics.
    """

    txn_date: date
    amount: Decimal
    description: str
    reference: str | None = None


class BankCsvError(ValueError):
    """Raised when a CSV can't be parsed."""


def detect_format(raw: str) -> BankCsvFormat:
    """Best-effort format detection.

    Header-less CSVs (CBA/ANZ/NAB) are detected by column count + date
    shape on the first row. The big distinguisher is column count:
    CBA=4, ANZ=3, NAB=6.
    """
    text = raw.strip()
    if not text:
        raise BankCsvError("empty file")

    # Westpac has a clear header row, easiest to detect.
    first_line = text.splitlines()[0].lower()
    if "narrative" in first_line and "debit amount" in first_line:
        return BankCsvFormat.WESTPAC

    # If the first line looks like a header (no date in col 0), fall
    # through to generic.
    first_cells = next(csv.reader(io.StringIO(text)))
    if not _looks_like_date(first_cells[0]):
        return BankCsvFormat.GENERIC

    # Header-less: distinguish by column count.
    count = len(first_cells)
    if count == 4:
        return BankCsvFormat.CBA
    if count == 3:
        return BankCsvFormat.ANZ
    if count >= 6:
        return BankCsvFormat.NAB
    # Bare 2-column exports fall back to generic and will likely raise.
    return BankCsvFormat.GENERIC


def parse_bank_csv(
    raw: str,
    *,
    fmt: BankCsvFormat | None = None,
) -> list[ParsedLine]:
    """Parse a bank CSV into ``ParsedLine`` rows.

    Format is auto-detected if not supplied.
    """
    if fmt is None:
        fmt = detect_format(raw)

    if fmt is BankCsvFormat.CBA:
        return _parse_cba(raw)
    if fmt is BankCsvFormat.ANZ:
        return _parse_anz(raw)
    if fmt is BankCsvFormat.NAB:
        return _parse_nab(raw)
    if fmt is BankCsvFormat.WESTPAC:
        return _parse_westpac(raw)
    return _parse_generic(raw)


# ----- format-specific parsers -----


def _parse_cba(raw: str) -> list[ParsedLine]:
    """CBA: Date, Amount, Description, Balance — no header."""
    lines = []
    for row in csv.reader(io.StringIO(raw)):
        if len(row) < 3:
            continue
        if not _looks_like_date(row[0]):
            continue
        lines.append(
            ParsedLine(
                txn_date=_parse_date(row[0]),
                amount=_parse_decimal(row[1]),
                description=row[2].strip(),
            )
        )
    return lines


def _parse_anz(raw: str) -> list[ParsedLine]:
    """ANZ: Date, Amount, Description — no header."""
    lines = []
    for row in csv.reader(io.StringIO(raw)):
        if len(row) < 3:
            continue
        if not _looks_like_date(row[0]):
            continue
        lines.append(
            ParsedLine(
                txn_date=_parse_date(row[0]),
                amount=_parse_decimal(row[1]),
                description=row[2].strip(),
            )
        )
    return lines


def _parse_nab(raw: str) -> list[ParsedLine]:
    """NAB: Date, Amount, "", "", Description, Balance — no header."""
    lines = []
    for row in csv.reader(io.StringIO(raw)):
        if len(row) < 5:
            continue
        if not _looks_like_date(row[0]):
            continue
        lines.append(
            ParsedLine(
                txn_date=_parse_date(row[0]),
                amount=_parse_decimal(row[1]),
                description=row[4].strip(),
            )
        )
    return lines


def _parse_westpac(raw: str) -> list[ParsedLine]:
    """Westpac: has a header; Debit Amount + Credit Amount are split."""
    return _parse_generic(raw)  # generic handler covers this shape


def _parse_generic(raw: str) -> list[ParsedLine]:
    """Header-driven parser; supports separate debit/credit columns."""
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise BankCsvError("CSV has no header row")

    lowered = {f.lower().strip(): f for f in reader.fieldnames}

    date_key = _first_match(lowered, _GENERIC_DATE_COLS)
    desc_key = _first_match(lowered, _GENERIC_DESC_COLS)
    amount_key = _first_match(lowered, _GENERIC_AMOUNT_COLS)
    debit_key = _first_match(lowered, _GENERIC_DEBIT_COLS)
    credit_key = _first_match(lowered, _GENERIC_CREDIT_COLS)

    if not date_key:
        raise BankCsvError("could not find a date column")
    if not desc_key:
        raise BankCsvError("could not find a description column")
    if not amount_key and not (debit_key or credit_key):
        raise BankCsvError("could not find an amount or debit/credit column")

    out: list[ParsedLine] = []
    for row in reader:
        raw_date = row.get(date_key, "").strip()
        if not raw_date:
            continue
        if amount_key:
            amount = _parse_decimal(row[amount_key])
        else:
            debit = _parse_decimal(row.get(debit_key, "") if debit_key else "")
            credit = _parse_decimal(row.get(credit_key, "") if credit_key else "")
            # Debit columns are usually positive in the file but denote
            # a withdrawal — flip sign so the caller always sees
            # "positive = money in".
            amount = credit - debit
        out.append(
            ParsedLine(
                txn_date=_parse_date(raw_date),
                amount=amount,
                description=row[desc_key].strip(),
            )
        )
    return out


# ----- helpers -----


_DATE_FORMATS = (
    "%d/%m/%Y",  # AU default
    "%d/%m/%y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d %b %Y",
)
_DATE_SHAPE = re.compile(r"^\s*\d{1,4}[/\-\s]\d{1,2}[/\-\s]\d{1,4}\s*$")


def _looks_like_date(s: str) -> bool:
    return bool(_DATE_SHAPE.match(s or ""))


def _parse_date(raw: str) -> date:
    raw = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise BankCsvError(f"unrecognised date: {raw!r}")


def _parse_decimal(raw: str) -> Decimal:
    """Tolerant decimal parser. Empty/missing → Decimal('0')."""
    if raw is None:
        return Decimal("0")
    s = raw.strip().replace(",", "").replace("$", "")
    if not s:
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise BankCsvError(f"unrecognised amount: {raw!r}") from e


def _first_match(
    lowered: dict[str, str], candidates: tuple[str, ...]
) -> str | None:
    for c in candidates:
        if c in lowered:
            return lowered[c]
    return None


__all__ = [
    "BankCsvError",
    "BankCsvFormat",
    "ParsedLine",
    "detect_format",
    "parse_bank_csv",
]
