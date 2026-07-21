"""TPAR (Taxable Payments Annual Report) BDE flat-file generator.

Renders a TPAR run as the ATO's fixed-length data file per the
**Electronic reporting specification — Taxable payments annual report
version 3.0.1** (spec version literal ``FPAIVV03.0``), lodged via
Online Services for Business file transfer (BDE). Archived spec:
``ci-host:~/records/saebooks/ato-artefacts/tpar-bde-spec-v301/``.

File layout (every record exactly 996 characters):

* ``IDENTREGISTER1`` — sender ABN, run type (T/P), report end date
* ``IDENTREGISTER2`` — sender name + contact
* ``IDENTREGISTER3`` — sender street/postal addresses + email
* per payer: ``IDENTITY`` (payer), ``SOFTWARE``, then one ``DPAIVS``
  record per payee
* ``FILE-TOTAL`` — count of ALL records in the file, itself included

Fill conventions carried by the spec (section 5/6):

* text (A/AN) fields are left-justified, blank-filled; optional text
  not present is all blanks
* numeric (N) fields are right-justified, zero-filled; optional
  numerics not present are all zeros
* dates are DDMMCCYY; optional dates not present are ``00000000``
* amounts are WHOLE DOLLARS with cents truncated, must not be signed
* payee ABN is zero-filled when the payee did not quote one
* the ATO prefers no CR/LF; when emitted they are a coupled pair on
  the end of every record (``crlf=True``)
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from saebooks.services.business_identifiers import validate as validate_identifier

RECORD_LENGTH = 996
SPEC_VERSION = "FPAIVV03.0"

# 6.20/6.37/6.55 — state/territory codes; OTH flags an overseas address.
STATE_CODES = frozenset({"ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA", "OTH"})
OVERSEAS_POSTCODE = "9999"


class TparBdeError(ValueError):
    """The inputs cannot produce a valid FPAIVV03.0 data file."""


@dataclass(frozen=True)
class BdeAddress:
    """One spec-shaped address (street or postal).

    Domestic: ``state`` is a real state code and ``postcode`` the real
    4-digit postcode. Overseas: ``state="OTH"``, ``postcode="9999"``
    and ``country`` must name the country (6.22/6.39/6.57).
    """

    line1: str
    suburb: str
    state: str
    postcode: str
    line2: str = ""
    country: str = ""

    @property
    def overseas(self) -> bool:
        return self.state.strip().upper() == "OTH"


@dataclass(frozen=True)
class BdeSender:
    """The entity lodging the file (IDENTREGISTER1-3). For a self-lodging
    business this is the reporting business itself."""

    abn: str
    name: str
    contact_name: str
    phone: str
    address: BdeAddress
    email: str = ""
    fax: str = ""
    file_reference: str = ""
    postal_address: BdeAddress | None = None  # None → street address reused


@dataclass(frozen=True)
class BdePayer:
    """The business whose contractor payments are reported (IDENTITY)."""

    abn: str
    financial_year: int  # CCYY of the FY END, e.g. 2026 for FY2025-26
    name: str
    address: BdeAddress
    branch_number: str = ""  # conditional — zero-filled when absent
    trading_name: str = ""
    contact_name: str = ""
    phone: str = ""
    fax: str = ""
    email: str = ""


@dataclass(frozen=True)
class BdePayee:
    """One reported contractor (DPAIVS record).

    Exactly one of the two naming shapes must be present (6.48/6.51):
    ``business_name`` for a non-individual, or ``family_name`` (+
    ``given_name`` unless a legal single name) for an individual.
    Amounts accept ``Decimal``/``int``/``str``; cents are truncated.
    """

    address: BdeAddress
    gross: Any
    tax_withheld: Any = 0
    gst: Any = 0
    abn: str = ""  # "" → zero-filled (payee did not quote an ABN)
    business_name: str = ""
    trading_name: str = ""
    family_name: str = ""
    given_name: str = ""
    other_given_name: str = ""
    phone: str = ""
    bsb: str = ""
    account_number: str = ""
    email: str = ""
    payment_type: str = "P"  # P=payments, G=grants (6.64)
    grant_payment_date: date | None = None  # grants only (6.65)
    grant_program_name: str = ""  # grants only (6.66)
    statement_by_supplier: bool = False  # 6.68
    amendment: bool = False  # 6.69 — O=original, A=amended
    nane: str = ""  # 6.70 — "", N, Y or U (Division 59 ITAA 1997)


# ---------------------------------------------------------------------------
# Field packers — one per spec field format (section 5, "Field format").
# ---------------------------------------------------------------------------

def _clean_text(value: Any) -> str:
    """Normalise to single-spaced printable ASCII (AN charset).

    The spec forbids leading blanks and doubled spaces in mandatory
    address fields; accented characters are transliterated (the file
    is a byte-per-character format with no encoding declaration).
    """
    text = " ".join(str(value or "").split())
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _an(value: Any, width: int, *, name: str, mandatory: bool = False) -> str:
    text = _clean_text(value)
    if mandatory and not text:
        raise TparBdeError(f"{name} is mandatory and blank")
    return text[:width].ljust(width)


def _n(value: Any, width: int, *, name: str, mandatory: bool = False) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if mandatory and not digits:
        raise TparBdeError(f"{name} is mandatory and blank")
    if len(digits) > width:
        raise TparBdeError(f"{name} has {len(digits)} digits; the field holds {width}")
    return digits.rjust(width, "0")


def _money(value: Any, *, name: str) -> str:
    """11-char whole-dollar amount — cents truncated (6.61-6.63)."""
    try:
        whole = int(Decimal(str(value)))
    except ArithmeticError as exc:
        raise TparBdeError(f"{name} is not a number: {value!r}") from exc
    if whole < 0:
        raise TparBdeError(f"{name} must not be negative (got {value})")
    return _n(whole, 11, name=name)


def _dt(value: date | None, *, name: str, mandatory: bool = False) -> str:
    if value is None:
        if mandatory:
            raise TparBdeError(f"{name} is mandatory and blank")
        return "00000000"
    return f"{value.day:02d}{value.month:02d}{value.year:04d}"


def _abn(value: str, *, name: str, allow_blank: bool = False) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        if allow_blank:  # payee did not quote an ABN — zero-fill (6.47)
            return "0" * 11
        raise TparBdeError(f"{name} is mandatory and blank")
    if validate_identifier("au_abn", digits) is not True:
        raise TparBdeError(f"{name} fails the ABN checksum: {value!r}")
    return digits


def _flag(value: Any, valid: str, *, name: str, blank_ok: bool = False) -> str:
    code = str(value or "").strip().upper()
    if not code and blank_ok:
        return " "
    if len(code) != 1 or code not in valid:
        raise TparBdeError(f"{name} must be one of {'/'.join(valid)} (got {value!r})")
    return code


def _address_fields(addr: BdeAddress, *, who: str, mandatory: bool = True) -> tuple[str, ...]:
    """Pack the recurring line1/line2/suburb/state/postcode/country run.

    Every record that carries an address uses the same 38/38/27/3/4/20
    shape. Overseas addresses must use state OTH + postcode 9999 and
    name the country (6.22/6.39/6.57).
    """
    state = addr.state.strip().upper() if addr.state else ""
    if mandatory or any((addr.line1, addr.suburb, state, addr.postcode)):
        if state not in STATE_CODES:
            raise TparBdeError(
                f"{who} state {addr.state!r} is not one of {sorted(STATE_CODES)}"
            )
        if addr.overseas:
            if not _clean_text(addr.country):
                raise TparBdeError(f"{who} is overseas (OTH) but has no country")
            if re.sub(r"\D", "", addr.postcode or "") != OVERSEAS_POSTCODE:
                raise TparBdeError(f"{who} is overseas (OTH) so postcode must be 9999")
    return (
        _an(addr.line1, 38, name=f"{who} address line 1", mandatory=mandatory),
        _an(addr.line2, 38, name=f"{who} address line 2"),
        _an(addr.suburb, 27, name=f"{who} suburb", mandatory=mandatory),
        _an(state, 3, name=f"{who} state", mandatory=mandatory),
        _n(addr.postcode, 4, name=f"{who} postcode", mandatory=mandatory),
        _an(addr.country, 20, name=f"{who} country"),
    )


def _record(*parts: str) -> str:
    body = "".join(parts)
    record = f"{RECORD_LENGTH:03d}{body}"
    if len(record) > RECORD_LENGTH:
        raise TparBdeError(
            f"internal: record overflows to {len(record)} characters"
        )
    return record.ljust(RECORD_LENGTH)


# ---------------------------------------------------------------------------
# Record builders (section 5, "Record specifications").
# ---------------------------------------------------------------------------

def _identregister1(sender: BdeSender, run_type: str, report_end: date) -> str:
    return _record(
        "IDENTREGISTER1".ljust(14),
        _abn(sender.abn, name="sender ABN"),
        _flag(run_type, "TP", name="run type"),
        _dt(report_end, name="report end date", mandatory=True),
        "P",  # data type (6.6)
        "C",  # type of report (6.7)
        "M",  # format of return media (6.8)
        _an(SPEC_VERSION, 10, name="spec version", mandatory=True),
    )


def _identregister2(sender: BdeSender) -> str:
    return _record(
        "IDENTREGISTER2".ljust(14),
        _an(sender.name, 200, name="sender name", mandatory=True),
        _an(sender.contact_name, 38, name="sender contact name", mandatory=True),
        _an(sender.phone, 15, name="sender contact telephone", mandatory=True),
        _an(sender.fax, 15, name="sender facsimile"),
        _an(sender.file_reference, 16, name="sender file reference"),
    )


def _identregister3(sender: BdeSender) -> str:
    postal = sender.postal_address
    return _record(
        "IDENTREGISTER3".ljust(14),
        *_address_fields(sender.address, who="sender"),
        *(
            _address_fields(postal, who="sender postal", mandatory=False)
            if postal is not None
            else (
                _an("", 38, name="sender postal line 1"),
                _an("", 38, name="sender postal line 2"),
                _an("", 27, name="sender postal suburb"),
                _an("", 3, name="sender postal state"),
                _n("", 4, name="sender postal postcode"),
                _an("", 20, name="sender postal country"),
            )
        ),
        _an(sender.email, 76, name="sender email"),
    )


def _identity(payer: BdePayer) -> str:
    return _record(
        "IDENTITY".ljust(8),
        _abn(payer.abn, name="payer ABN"),
        _n(payer.branch_number, 3, name="payer branch number"),
        _n(payer.financial_year, 4, name="financial year", mandatory=True),
        _an(payer.name, 200, name="payer name", mandatory=True),
        _an(payer.trading_name, 200, name="payer trading name"),
        *_address_fields(payer.address, who="payer"),
        _an(payer.contact_name, 38, name="payer contact name"),
        _an(payer.phone, 15, name="payer contact telephone"),
        _an(payer.fax, 15, name="payer contact facsimile"),
        _an(payer.email, 76, name="payer contact email"),
    )


def _software(developer_name: str) -> str:
    """6.45 — in-house products report ``INHOUSE`` + the developing org."""
    return _record(
        "SOFTWARE".ljust(8),
        _an(f"INHOUSE {_clean_text(developer_name)}", 80,
            name="software product type", mandatory=True),
    )


def _dpaivs(payee: BdePayee, index: int) -> str:
    who = f"payee #{index}"
    has_person = bool(_clean_text(payee.family_name))
    has_org = bool(_clean_text(payee.business_name))
    if not has_person and not has_org:
        raise TparBdeError(f"{who} needs a business name or a family name (6.48/6.51)")
    # 6.48/6.49 — individuals need a first given name unless they have a
    # legal single name; a given name without a family name is invalid.
    if _clean_text(payee.given_name) and not has_person:
        raise TparBdeError(f"{who} has a given name but no family name")
    payment_type = _flag(payee.payment_type, "GP", name=f"{who} payment type")
    if payment_type == "G" and payee.grant_payment_date is None:
        raise TparBdeError(f"{who} is a grant (G) so date of grant payment is required")
    return _record(
        "DPAIVS",
        _abn(payee.abn, name=f"{who} ABN", allow_blank=True),
        _an(payee.family_name, 30, name=f"{who} family name"),
        _an(payee.given_name, 15, name=f"{who} first given name"),
        _an(payee.other_given_name, 15, name=f"{who} second given name"),
        _an(payee.business_name, 200, name=f"{who} business name"),
        _an(payee.trading_name, 200, name=f"{who} trading name"),
        *_address_fields(payee.address, who=who),
        _an(payee.phone, 15, name=f"{who} telephone"),
        _n(payee.bsb, 6, name=f"{who} BSB"),
        _n(payee.account_number, 9, name=f"{who} account number"),
        _money(payee.gross, name=f"{who} gross amount paid"),
        _money(payee.tax_withheld, name=f"{who} total tax withheld"),
        _money(payee.gst, name=f"{who} total GST"),
        payment_type,
        _dt(payee.grant_payment_date, name=f"{who} date of grant payment"),
        _an(payee.grant_program_name, 200, name=f"{who} grant program name"),
        _an(payee.email, 76, name=f"{who} email"),
        "Y" if payee.statement_by_supplier else "N",
        "A" if payee.amendment else "O",
        _flag(payee.nane, "NYU", name=f"{who} NANE", blank_ok=True),
    )


def _file_total(record_count: int) -> str:
    return _record(
        "FILE-TOTAL".ljust(10),
        _n(record_count, 8, name="number of records", mandatory=True),
    )


def build_tpar_bde_file(
    sender: BdeSender,
    payer: BdePayer,
    payees: list[BdePayee],
    *,
    software_developer: str,
    run_type: str = "P",
    report_end_date: date | None = None,
    crlf: bool = False,
) -> bytes:
    """Render one payer's TPAR as a complete FPAIVV03.0 data file.

    ``report_end_date`` defaults to 30 June of ``payer.financial_year``
    (the FY end — 6.5 requires it within the financial year). The spec
    allows multiple payers per file; the engine lodges one company per
    run, so this builder takes exactly one. ``run_type="T"`` produces a
    test file for the BDE file transfer test facility.
    """
    if not payees:
        raise TparBdeError("a TPAR file needs at least one payee")
    gross_zero = [i for i, p in enumerate(payees, start=1)
                  if int(Decimal(str(p.gross))) <= 0]
    if gross_zero:
        raise TparBdeError(
            "gross amount paid must be greater than zero (6.61) — "
            f"payee #{', #'.join(map(str, gross_zero))}"
        )
    report_end = report_end_date or date(payer.financial_year, 6, 30)
    if not (date(payer.financial_year - 1, 7, 1) <= report_end
            <= date(payer.financial_year, 6, 30)):
        raise TparBdeError(
            f"report end date {report_end} is outside FY{payer.financial_year} (6.5)"
        )

    records = [
        _identregister1(sender, run_type, report_end),
        _identregister2(sender),
        _identregister3(sender),
        _identity(payer),
        _software(software_developer),
        *(_dpaivs(p, i) for i, p in enumerate(payees, start=1)),
    ]
    records.append(_file_total(len(records) + 1))  # count includes FILE-TOTAL

    for r in records:
        assert len(r) == RECORD_LENGTH
    terminator = "\r\n" if crlf else ""
    return (terminator.join(records) + terminator if crlf else "".join(records)).encode("ascii")
