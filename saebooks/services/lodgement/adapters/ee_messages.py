"""Parse the e-MTA ``operationAccepted`` / ``operationRejected`` feedback messages.

These are the machine-channel response messages EMTA generates after receiving a
KMD / KMD-INF declaration. They are pinned by the in-tree XSDs
``emta-schemas/operationaccepted.xsd`` and ``operationrejected.xsd`` (copied into
``tests/fixtures/emta_schemas/`` for the validation tests).

Two-channel nuance (surface it, don't paper over it)
----------------------------------------------------

``operationAccepted`` / ``operationRejected`` are the **2025 KMD/KMD-INF** machine
channel messages — they carry ``vatPayable`` (lahter 12), ``overpaidVat`` (lahter
13) and ``declarationType`` (normal=1 / bankruptcy=2). They are **NOT** the 2027
KMD3 XBRL-GL async feedback report, whose exact wrapper (the koondvaade aggregate
view returned by ``GET /return-data/{uuid}``) is UNVERIFIED — the X-tee interfacing
guide §2 marks it "täpsem info lisandub" (more detail to follow).

Per the build plan (Module 3 §3.1) we build the state machine against the response
contract we actually *have* (these XSDs), parse them faithfully, and mark the
KMD3-specific poll-body wrapper as UNVERIFIED where it is (see ``ee_client.poll``).
When EMTA publishes the KMD3 feedback schema, the *envelope* around these messages
may change; the ``operationAccepted``/``operationRejected`` element shapes parsed
here are the concrete, pinned contract.

No namespace
------------

Both XSDs declare ``elementFormDefault="qualified"`` but define **no
targetNamespace** — so every element sits in the empty namespace. We therefore
match by local element name and never assume a prefix. Parsing is tolerant (locate
elements by name); XSD *validation* is an explicit opt-in (``validate_against``)
used by the tests, not a runtime requirement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lxml import etree

# Root element local-names that identify each message kind.
ACCEPTED_ROOT = "operationAccepted"
REJECTED_ROOT = "operationRejected"


@dataclass(frozen=True, slots=True)
class FunctionalError:
    """One business-rule / data error from a feedback message.

    Mirrors the XSD ``FunctionalError`` complexType (all three fields optional):
    ``errorPointer`` (rule code), ``originalAttributeValue`` (the offending
    value), ``errorDescription`` (human text).
    """

    error_pointer: str | None = None
    original_value: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ReturnFeedback:
    """The parsed outcome of a KMD3 feedback report.

    ``accepted`` is the discriminator: ``True`` for ``operationAccepted``,
    ``False`` for ``operationRejected``. On acceptance, ``vat_payable`` (lahter
    12) / ``overpaid_vat`` (lahter 13) carry EMTA's server-computed bottom line
    (either may be None; the XSD makes both optional) and ``declaration_state``
    is the free-form olek string. On rejection those are None and ``xml_errors``
    holds the XML-structure error descriptions.

    ``functional_errors`` (business-rule errors) can be present on *either*
    message — an accepted declaration may still carry warnings/functional
    notes, and a rejected one lists what failed.
    """

    request_id: str
    accepted: bool
    vat_payable: Decimal | None = None
    overpaid_vat: Decimal | None = None
    declaration_state: str | None = None
    declaration_type: str | None = None
    taxpayer_reg_code: str | None = None
    submitter_person_code: str | None = None
    year: int | None = None
    month: int | None = None
    functional_errors: list[FunctionalError] = field(default_factory=list)
    xml_errors: list[str] = field(default_factory=list)


class EEMessageParseError(ValueError):
    """The bytes were not a well-formed operationAccepted/Rejected message."""


def _text(el: etree._Element | None) -> str | None:
    if el is None:
        return None
    t = el.text
    if t is None:
        return None
    t = t.strip()
    return t or None


def _find(root: etree._Element, name: str) -> etree._Element | None:
    """First direct/descendant child with matching local name (namespace-agnostic)."""
    for child in root.iter():
        if (
            child is not root
            and isinstance(child.tag, str)
            and etree.QName(child).localname == name
        ):
            return child
    return None


def _find_all(root: etree._Element, name: str) -> list[etree._Element]:
    return [
        child
        for child in root.iter()
        if child is not root
        and isinstance(child.tag, str)
        and etree.QName(child).localname == name
    ]


def _decimal(el: etree._Element | None) -> Decimal | None:
    t = _text(el)
    if t is None:
        return None
    try:
        return Decimal(t)
    except (InvalidOperation, ValueError):
        return None


def _int(el: etree._Element | None) -> int | None:
    t = _text(el)
    if t is None:
        return None
    try:
        return int(t)
    except ValueError:
        return None


def _parse_functional_errors(root: etree._Element) -> list[FunctionalError]:
    out: list[FunctionalError] = []
    for fe in _find_all(root, "functionalError"):
        out.append(
            FunctionalError(
                error_pointer=_text(_find(fe, "errorPointer")),
                original_value=_text(_find(fe, "originalAttributeValue")),
                description=_text(_find(fe, "errorDescription")),
            )
        )
    return out


def _root_localname(xml: bytes) -> tuple[etree._Element, str]:
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:  # pragma: no cover - defensive
        raise EEMessageParseError(f"not well-formed XML: {exc}") from exc
    return root, etree.QName(root).localname


def parse_operation_accepted(xml: bytes) -> ReturnFeedback:
    """Parse an ``operationAccepted`` message into a :class:`ReturnFeedback`.

    Raises :class:`EEMessageParseError` if the root is not ``operationAccepted``
    or ``requestId`` (the one mandatory correlator) is absent.
    """
    root, local = _root_localname(xml)
    if local != ACCEPTED_ROOT:
        raise EEMessageParseError(
            f"expected <{ACCEPTED_ROOT}> root, got <{local}>"
        )
    request_id = _text(_find(root, "requestId"))
    if request_id is None:
        raise EEMessageParseError("operationAccepted missing requestId")
    return ReturnFeedback(
        request_id=request_id,
        accepted=True,
        vat_payable=_decimal(_find(root, "vatPayable")),
        overpaid_vat=_decimal(_find(root, "overpaidVat")),
        declaration_state=_text(_find(root, "declarationState")),
        declaration_type=_text(_find(root, "declarationType")),
        taxpayer_reg_code=_text(_find(root, "taxPayerRegCode")),
        submitter_person_code=_text(_find(root, "submitterPersonCode")),
        year=_int(_find(root, "year")),
        month=_int(_find(root, "month")),
        functional_errors=_parse_functional_errors(root),
        xml_errors=[],
    )


def parse_operation_rejected(xml: bytes) -> ReturnFeedback:
    """Parse an ``operationRejected`` message into a :class:`ReturnFeedback`.

    ``accepted`` is ``False``; ``xml_errors`` holds the XML-structure error
    descriptions and ``functional_errors`` the business-rule failures.
    """
    root, local = _root_localname(xml)
    if local != REJECTED_ROOT:
        raise EEMessageParseError(
            f"expected <{REJECTED_ROOT}> root, got <{local}>"
        )
    request_id = _text(_find(root, "requestId"))
    if request_id is None:
        raise EEMessageParseError("operationRejected missing requestId")
    xml_errors = [
        _text(_find(xe, "errorDescription")) or ""
        for xe in _find_all(root, "xmlError")
    ]
    return ReturnFeedback(
        request_id=request_id,
        accepted=False,
        functional_errors=_parse_functional_errors(root),
        xml_errors=[e for e in xml_errors if e],
    )


def parse_feedback_message(xml: bytes) -> ReturnFeedback:
    """Dispatch on the root element to the right parser.

    This is the entry point ``ee_client.poll`` calls once it has extracted the
    ``operationAccepted``/``operationRejected`` message from the feedback
    report. Raises :class:`EEMessageParseError` for any other root.
    """
    _root, local = _root_localname(xml)
    if local == ACCEPTED_ROOT:
        return parse_operation_accepted(xml)
    if local == REJECTED_ROOT:
        return parse_operation_rejected(xml)
    raise EEMessageParseError(
        f"unrecognised feedback root <{local}> — expected "
        f"<{ACCEPTED_ROOT}> or <{REJECTED_ROOT}>"
    )


def validate_against(xml: bytes, xsd_path: str | Path) -> None:
    """Opt-in XSD validation against an in-tree schema.

    Used by the tests to prove authored sample instances conform to the real
    ``operationaccepted.xsd`` / ``operationrejected.xsd``. NOT called on the
    runtime parse path (a live feedback message we already trust from EMTA;
    validating it would add a hard XSD dependency to the request path for no
    safety win). Raises ``lxml`` ``DocumentInvalid`` with a precise message.
    """
    schema = etree.XMLSchema(etree.parse(str(xsd_path)))
    schema.assertValid(etree.fromstring(xml))
