"""TPAR (Taxable Payments Annual Report) SBR document generator.

Maps the engine's TPAR run (``services/tpar.py`` → ``tpar_runs`` +
``tpar_lines``) onto the **TPAR.0003 (2021)** message pair for the
lodge-server's SBR2 (ebMS3) transport.

Serialisation — plain XML, NOT XBRL
-----------------------------------
TPAR.0003 is a plain-XML service (the public taxonomy server only hosts the
retired XBRL-era ``tpar.0001``). One submission is a **document set**:

* one parent ``TPAR`` (INT sender/intermediary block + RP reporting party),
  namespace ``http://www.sbr.gov.au/ato/tpar``; and
* one child ``TPARPYE`` document per payee (contractor), namespace
  ``http://www.sbr.gov.au/ato/tparpye``.

On the wire the parent and children travel as separate ebMS3/AS4 message
parts — packaging them is the lodge-server's job; this module returns the
parts as :class:`TparDocuments`.

Element names, nesting, and ordering are derived from the official **ATO
TPAR.0003 2021 Conformance Suite v1.1** (17 parent + 43 payee request
payloads across the BULK + BATCH scenarios) and are pinned by the
round-trip harness in ``tests_conformance/test_sbr_conformance.py`` —
every official request regenerates byte-equivalently from parsed inputs.

Conventions carried by the official payloads:

* **Amounts are whole dollars** (no cents) — ``20000``, not ``20000.00``.
* **Empty elements are significant** — several samples carry ``<Line2T/>``;
  a field set to ``""`` emits an empty element, ``None`` omits it.
* The ``INT`` block carries the (only) ``Declaration``; every official
  sample includes it. Whether a self-lodging RP may omit INT is a
  ``TODO(MST)`` — the TPAR.0003 Message Structure Table (EO doc library)
  is the authority; until confirmed, callers pass the sender's details as
  the intermediary (for a self-lodger, the RP's own details).
* Validate and Submit requests are structurally identical — the
  interaction (``tpar.0003.2021.validate`` vs ``.submit``) is selected at
  the ebMS3 layer, not in the document.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from lxml import etree

# TPAR.0003 (2021) namespaces — verified against the ATO conformance payloads
# (which carry them as default namespaces with no xsi:schemaLocation).
TPAR_NS = "http://www.sbr.gov.au/ato/tpar"
TPARPYE_NS = "http://www.sbr.gov.au/ato/tparpye"


class TparDocumentError(ValueError):
    """The inputs are missing fields TPAR.0003 requires."""


@dataclass(frozen=True)
class TparDocuments:
    """One TPAR submission's document set (parent + one child per payee)."""

    parent: bytes
    payees: tuple[bytes, ...]

    @property
    def record_count(self) -> int:
        return len(self.payees)

    def to_envelope_bundle(self) -> bytes:
        """Deterministic zip bundle for the engine → lodge-server handoff.

        Same convention as ``PayEventDocuments.to_envelope_bundle``: fixed
        entry names (``tpar.xml``, ``tparpye-0001.xml`` …) and a fixed epoch
        so identical documents always hash identically (the submit path's
        idempotency key is derived from the envelope hash).
        """
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
            entries = [("tpar.xml", self.parent)] + [
                (f"tparpye-{i:04d}.xml", doc)
                for i, doc in enumerate(self.payees, start=1)
            ]
            for name, data in entries:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                zf.writestr(info, data)
        return buf.getvalue()


@dataclass(frozen=True)
class TparPhone:
    """ATO split telephone/facsimile representation (area code + number)."""

    area_code: str
    number: str


@dataclass(frozen=True)
class TparAddress:
    """Address block. ``None`` omits an element; ``""`` emits it empty.

    The canonical child order (merged from every official sample) is:
    OverseasAddressI, Line1T, Line2T, LocalityNameT, PostcodeT,
    StateOrTerritoryC, CountryC. Domestic addresses carry postcode + state;
    overseas addresses drop them and carry the country code instead.
    """

    line1: str
    locality: str
    overseas: bool = False
    line2: str | None = None
    postcode: str | None = None
    state: str | None = None
    country: str | None = None  # lowercase ISO-3166 alpha-2, e.g. "au"


@dataclass(frozen=True)
class TparIntermediary:
    """The TPAR ``INT`` block — the sender lodging the report.

    Unlike the AS.0004 ``INT`` (ABN + TAN + declaration only), TPAR's
    carries the sender's full identity and contact set, and it holds the
    submission's only Declaration. ``tax_agent_number`` stays ``None`` for
    a self-lodging business sending its own report.
    """

    abn: str
    organisation_name: str
    contact_name: str
    business_address: TparAddress
    postal_address: TparAddress
    email: str
    phone: TparPhone
    fax: TparPhone | None = None
    tax_agent_number: str | None = None
    declaration_accepted: bool = True
    declaration_signature_date: str = ""  # ISO date
    declaration_signatory: str = ""


@dataclass(frozen=True)
class TparReportingParty:
    """The TPAR ``RP`` block — the business whose contractors are reported."""

    abn: str
    branch_code: str  # OrganisationDetailsOrganisationBranchC, e.g. "1"
    period_start: str  # ISO date, FY start (1 July)
    period_end: str  # ISO date, FY end (30 June)
    financial_year: str  # TargetFinancialY, e.g. "2021" for FY2020-21
    organisation_name: str
    address: TparAddress
    contact_name: str
    trading_name: str | None = None
    phone: TparPhone | None = None
    fax: TparPhone | None = None
    email: str | None = None


@dataclass(frozen=True)
class TparPayeeRecord:
    """One reported contractor — a ``TPARPYE`` child document.

    Amounts accept ``Decimal``/``int``/``str`` and are rendered as whole
    dollars (ATO TPAR truncates cents). ``gst`` may be ``None`` only where
    the payee is not GST-registered territory (one official scenario omits
    the element entirely).
    """

    gross: Any  # IncomeBusinessPaymentGrossA
    tax_withheld: Any  # IncomeTaxPayAsYouGoWithholdingTaxWithheldA
    address: TparAddress
    abn: str | None = None
    bsb: str | None = None
    account_number: str | None = None
    gst: Any | None = None  # GoodsAndServicesTaxLiabilityA
    abn_not_provided: bool = False
    payment_type: str = "Payment"  # IncomeTaxPaymentTypeC
    amendment: bool = False
    family_name: str | None = None
    given_name: str | None = None
    other_given_name: str | None = None
    organisation_name: str | None = None
    trading_name: str | None = None
    phone: TparPhone | None = None
    # Government-entity grant reporting (unused by business TPAR):
    grant_program_name: str | None = None
    grant_payment_date: str | None = None
    grant_division_59: str | None = None  # GovernmentFundingITAADivision59IndicatorC


def _dollars(value: Any) -> str:
    """TPAR monetary values are whole dollars — cents are truncated."""
    return str(int(Decimal(str(value))))


def _bool(value: Any) -> str:
    return "true" if bool(value) else "false"


def _leaf(parent: etree._Element, ns: str, name: str, text: str) -> None:
    el = etree.SubElement(parent, etree.QName(ns, name))
    el.text = text if text != "" else None


def _opt_leaf(parent: etree._Element, ns: str, name: str, value: Any) -> None:
    """Emit when the value is present — ``""`` emits an EMPTY element
    (several official payloads carry ``<Line2T/>``); ``None`` omits."""
    if value is not None:
        _leaf(parent, ns, name, str(value))


def _phone(parent: etree._Element, ns: str, name: str, value: TparPhone | None) -> None:
    if value is None:
        return
    el = etree.SubElement(parent, etree.QName(ns, name))
    _leaf(el, ns, "AreaC", value.area_code)
    _leaf(el, ns, "MinimalN", value.number)


def _address(parent: etree._Element, ns: str, name: str, addr: TparAddress) -> None:
    el = etree.SubElement(parent, etree.QName(ns, name))
    _leaf(el, ns, "OverseasAddressI", _bool(addr.overseas))
    _leaf(el, ns, "Line1T", addr.line1)
    _opt_leaf(el, ns, "Line2T", addr.line2)
    _leaf(el, ns, "LocalityNameT", addr.locality)
    _opt_leaf(el, ns, "PostcodeT", addr.postcode)
    _opt_leaf(el, ns, "StateOrTerritoryC", addr.state)
    _opt_leaf(el, ns, "CountryC", addr.country)


def build_tpar_report_part(
    rp: TparReportingParty,
    intermediary: TparIntermediary | None = None,
) -> bytes:
    """Render the parent ``TPAR`` document (INT + RP blocks)."""
    missing = [
        label
        for label, value in (
            ("rp.abn", rp.abn),
            ("rp.organisation_name", rp.organisation_name),
            ("rp.period_start", rp.period_start),
            ("rp.period_end", rp.period_end),
            ("rp.financial_year", rp.financial_year),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise TparDocumentError(
            "TPAR reporting party is missing required fields: " + ", ".join(missing)
        )

    ns = TPAR_NS
    root = etree.Element(etree.QName(ns, "TPAR"), nsmap={None: ns})

    if intermediary is not None:
        it = intermediary
        int_el = etree.SubElement(root, etree.QName(ns, "INT"))
        _leaf(int_el, ns, "AustralianBusinessNumberId", it.abn)
        _opt_leaf(int_el, ns, "TaxAgentNumberId", it.tax_agent_number)
        _leaf(int_el, ns, "OrganisationNameDetailsOrganisationalNameT", it.organisation_name)
        _phone(int_el, ns, "ElectronicContactTelephone", it.phone)
        _leaf(int_el, ns, "PersonUnstructuredNameFullNameT", it.contact_name)
        _phone(int_el, ns, "ElectronicContactFacsimile", it.fax)
        _address(int_el, ns, "AddressDetailsBusiness", it.business_address)
        _address(int_el, ns, "AddressDetailsPostal", it.postal_address)
        _leaf(int_el, ns, "ElectronicContactElectronicMailAddressT", it.email)
        decl = etree.SubElement(int_el, etree.QName(ns, "Declaration"))
        _leaf(decl, ns, "StatementAcceptedI", _bool(it.declaration_accepted))
        _leaf(decl, ns, "SignatureD", it.declaration_signature_date)
        _leaf(decl, ns, "SignatoryIdentifierT", it.declaration_signatory)

    rp_el = etree.SubElement(root, etree.QName(ns, "RP"))
    _leaf(rp_el, ns, "AustralianBusinessNumberId", rp.abn)
    _leaf(rp_el, ns, "OrganisationDetailsOrganisationBranchC", rp.branch_code)
    _leaf(rp_el, ns, "PeriodStartD", rp.period_start)
    _leaf(rp_el, ns, "PeriodEndD", rp.period_end)
    _leaf(rp_el, ns, "TargetFinancialY", rp.financial_year)
    _leaf(rp_el, ns, "OrganisationNameDetailsMainOrganisationalNameT", rp.organisation_name)
    _opt_leaf(rp_el, ns, "OrganisationNameDetailsTradingOrganisationalNameT", rp.trading_name)
    _address(rp_el, ns, "AddressDetails", rp.address)
    _leaf(rp_el, ns, "PersonUnstructuredNameFullNameT", rp.contact_name)
    _phone(rp_el, ns, "ElectronicContactTelephone", rp.phone)
    _phone(rp_el, ns, "ElectronicContactFacsimile", rp.fax)
    _opt_leaf(rp_el, ns, "ElectronicContactElectronicMailAddressT", rp.email)

    return bytes(etree.tostring(root, xml_declaration=False, encoding="UTF-8", pretty_print=True))


def build_tpar_payee_part(payee: TparPayeeRecord) -> bytes:
    """Render one ``TPARPYE`` child document."""
    missing: list[str] = []
    if payee.gross is None:
        missing.append("gross")
    if payee.tax_withheld is None:
        missing.append("tax_withheld")
    has_person = bool(str(payee.family_name or "").strip())
    has_org = bool(str(payee.organisation_name or "").strip())
    if not has_person and not has_org:
        missing.append("family_name or organisation_name")
    if missing:
        raise TparDocumentError(
            "TPAR payee is missing required fields: " + ", ".join(missing)
        )

    ns = TPARPYE_NS
    root = etree.Element(etree.QName(ns, "TPARPYE"), nsmap={None: ns})
    p = etree.SubElement(root, etree.QName(ns, "Payee"))

    _opt_leaf(p, ns, "AustralianBusinessNumberId", payee.abn)
    _opt_leaf(p, ns, "FinancialInstitutionAccountBankStateBranchN", payee.bsb)
    _opt_leaf(p, ns, "FinancialInstitutionAccountFinancialInstitutionAccountN", payee.account_number)
    _leaf(p, ns, "IncomeBusinessPaymentGrossA", _dollars(payee.gross))
    _leaf(p, ns, "IncomeTaxPayAsYouGoWithholdingTaxWithheldA", _dollars(payee.tax_withheld))
    if payee.gst is not None:
        _leaf(p, ns, "GoodsAndServicesTaxLiabilityA", _dollars(payee.gst))
    _opt_leaf(p, ns, "GovernmentFundingGrantProgramNameT", payee.grant_program_name)
    _opt_leaf(p, ns, "GovernmentFundingGrantPaymentD", payee.grant_payment_date)
    _leaf(p, ns, "IncomeTaxPayAsYouGoWithholdingABNNotProvidedStatementI", _bool(payee.abn_not_provided))
    _leaf(p, ns, "IncomeTaxPaymentTypeC", payee.payment_type)
    _opt_leaf(p, ns, "GovernmentFundingITAADivision59IndicatorC", payee.grant_division_59)
    _leaf(p, ns, "AmendmentI", _bool(payee.amendment))

    if has_person:
        names = etree.SubElement(p, etree.QName(ns, "PersonNameDetails"))
        _leaf(names, ns, "FamilyNameT", str(payee.family_name))
        _opt_leaf(names, ns, "GivenNameT", payee.given_name)
        _opt_leaf(names, ns, "OtherGivenNameT", payee.other_given_name)

    _opt_leaf(p, ns, "OrganisationNameDetailsMainOrganisationalNameT", payee.organisation_name)
    _opt_leaf(p, ns, "OrganisationNameDetailsTradingOrganisationalNameT", payee.trading_name)
    _address(p, ns, "AddressDetails", payee.address)
    _phone(p, ns, "ElectronicContactTelephone", payee.phone)

    return bytes(etree.tostring(root, xml_declaration=False, encoding="UTF-8", pretty_print=True))


def build_tpar_document(
    rp: TparReportingParty,
    payees: list[TparPayeeRecord],
    intermediary: TparIntermediary | None = None,
) -> TparDocuments:
    """Render a TPAR submission as its TPAR.0003 document set."""
    if not payees:
        raise TparDocumentError("a TPAR submission needs at least one payee")
    return TparDocuments(
        parent=build_tpar_report_part(rp, intermediary),
        payees=tuple(build_tpar_payee_part(p) for p in payees),
    )
