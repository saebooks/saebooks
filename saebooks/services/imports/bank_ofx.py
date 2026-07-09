"""OFX 1.x (SGML) + 2.x (XML) parser.

Tolerant enough to handle the OFX files emitted by the big-four AU
banks + MYOB/Xero exports. We don't care about investment statements
or credit cards with nested securities — just the STMTTRN block which
gives us: TRNTYPE, DTPOSTED, TRNAMT, FITID, NAME, MEMO.

OFX 1.x is SGML (unclosed tags are legal). Instead of parsing SGML
properly we flatten tag+text pairs with a tiny regex state machine —
works because STMTTRN fields are all scalar.

OFX 2.x has a proper XML header and can be parsed with stdlib ElementTree.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from saebooks.services.imports.bank_csv import ParsedLine


class OfxError(ValueError):
    """Raised when an OFX blob can't be parsed."""


def parse_ofx(raw: str | bytes) -> list[ParsedLine]:
    """Parse OFX 1.x or 2.x content into ``ParsedLine`` rows."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text:
        raise OfxError("empty file")

    # OFX 2.x starts with <?xml ...?>.
    if text.startswith("<?xml"):
        return _parse_ofx2(text)
    return _parse_ofx1(text)


# ----- OFX 2.x (XML) -----


def _parse_ofx2(text: str) -> list[ParsedLine]:
    # The real OFX body starts at <OFX>. Anything before it is
    # HTTP-ish metadata.
    m = re.search(r"<OFX.*", text, re.DOTALL)
    if not m:
        raise OfxError("no <OFX> root found")
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError as e:
        raise OfxError(f"XML parse error: {e}") from e
    lines = []
    for txn in root.iter("STMTTRN"):
        lines.append(_txn_to_line(_collect_scalar_children(txn)))
    return [line for line in lines if line is not None]


def _collect_scalar_children(el: ET.Element) -> dict[str, str]:
    return {child.tag: (child.text or "").strip() for child in el}


# ----- OFX 1.x (SGML) -----


# Matches SGML-style flat tags: <NAME>value (optional closing tag
# immediately after). Groups: tag, value.
_OFX1_TAG = re.compile(r"<(/?)([A-Z0-9.]+)>([^<\r\n]*)", re.IGNORECASE)


def _parse_ofx1(text: str) -> list[ParsedLine]:
    # Walk the tokens, tracking the innermost STMTTRN block so we only
    # emit lines from inside one.
    in_stmt = False
    current: dict[str, str] = {}
    out: list[ParsedLine] = []
    for m in _OFX1_TAG.finditer(text):
        closing, tag, value = m.group(1), m.group(2).upper(), m.group(3).strip()
        if tag == "STMTTRN":
            if closing:
                # End of txn: emit.
                line = _txn_to_line(current)
                if line is not None:
                    out.append(line)
                current = {}
                in_stmt = False
            else:
                in_stmt = True
                current = {}
        elif in_stmt and not closing and value:
            current[tag] = value
    return out


def _txn_to_line(tags: dict[str, str]) -> ParsedLine | None:
    dt_raw = tags.get("DTPOSTED") or tags.get("DTAVAIL") or ""
    amt_raw = tags.get("TRNAMT", "")
    if not dt_raw or not amt_raw:
        return None
    return ParsedLine(
        txn_date=_parse_ofx_date(dt_raw),
        amount=_parse_ofx_amount(amt_raw),
        description=tags.get("NAME") or tags.get("MEMO") or "",
        reference=tags.get("FITID") or None,
    )


def _parse_ofx_date(raw: str) -> date:
    """OFX dates are YYYYMMDD[HHMMSS[.xxx][tz]]. We take the first 8."""
    s = raw.strip()
    if len(s) < 8:
        raise OfxError(f"unrecognised OFX date: {raw!r}")
    try:
        return datetime.strptime(s[:8], "%Y%m%d").date()
    except ValueError as e:
        raise OfxError(f"unrecognised OFX date: {raw!r}") from e


def _parse_ofx_amount(raw: str) -> Decimal:
    try:
        return Decimal(raw.strip())
    except InvalidOperation as e:
        raise OfxError(f"unrecognised OFX amount: {raw!r}") from e


__all__ = ["OfxError", "parse_ofx"]
