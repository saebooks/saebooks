"""Minimal XBRL instance builder for SBR (Standard Business Reporting) documents.

The ATO lodges Activity Statements (BAS/IAS) — and most SBR services — as an
**XBRL payload** carried over the SBR ebMS3/AS4 channel (see sbr.gov.au). This
module builds a well-formed XBRL instance from a flat list of facts plus a
single reporting context (the entity ABN + the period) and a currency unit.

What this is — and is NOT
-------------------------
This produces a *structurally* valid XBRL instance: `xbrli:xbrl` root, a
`link:schemaRef`, one period context scoped to the lodging entity's ABN, an
AUD unit, and one element per fact. That structure is stable and correct.

It is NOT yet *conformant*. The authoritative element/concept QNames, the
target namespace, and the `schemaRef` href come from the **SBR Activity
Statement taxonomy + the form Message Implementation Guide (MIG)**, which are
gated behind ATO DSP (Digital Service Provider) registration. The concept
names used by the callers in this package (see ``bas._AS_CONCEPTS``) are
PLACEHOLDERS modelled on the public BAS label codes; they MUST be replaced
with the taxonomy concepts from the MIG and validated against the ATO **EVTE**
(External Vendor Test Environment) before any real lodgement.

Boundary: this engine produces the *business document* only. Signing with the
ATO Machine Credential and the ebMS3/AS4 transport to the ATO happen in the
private **lodge-server** (see ``docs/contracts/lodge-server.md``), which
receives this document as ``envelope_xml`` (base64) + ``envelope_hash``.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from lxml import etree

# Core XBRL 2.1 namespaces (stable, public).
XBRLI = "http://www.xbrl.org/2003/instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
ISO4217 = "http://www.xbrl.org/2003/iso4217"

# ATO uses the ABN as the entity identifier scheme.
ABN_SCHEME = "http://www.ato.gov.au/abn"


@dataclass(frozen=True)
class ReportingContext:
    """The single XBRL context every fact in an Activity Statement shares."""

    abn: str
    period_start: date
    period_end: date
    context_id: str = "ctx-period"
    unit_id: str = "AUD"


@dataclass(frozen=True)
class Fact:
    """One reported value: a taxonomy concept (ns + local name) and its amount."""

    concept_ns: str
    concept_name: str
    value: Decimal
    decimals: str = "0"  # AS monetary labels are whole dollars


def _money(value: Decimal) -> str:
    """Whole-dollar string for an AS monetary fact (ATO labels truncate cents)."""
    return str(int(Decimal(value).quantize(Decimal("1"))))


def build_instance(
    facts: list[Fact],
    ctx: ReportingContext,
    *,
    taxonomy_ns: str,
    taxonomy_prefix: str,
    schema_ref: str,
) -> bytes:
    """Build an XBRL instance and return pretty-printed UTF-8 bytes.

    ``taxonomy_ns`` / ``taxonomy_prefix`` / ``schema_ref`` identify the SBR
    form taxonomy and are PLACEHOLDERS until sourced from the MIG.
    """
    nsmap = {
        "xbrli": XBRLI,
        "link": LINK,
        "xlink": XLINK,
        "iso4217": ISO4217,
        taxonomy_prefix: taxonomy_ns,
    }
    root = etree.Element(etree.QName(XBRLI, "xbrl"), nsmap=nsmap)

    sref = etree.SubElement(root, etree.QName(LINK, "schemaRef"))
    sref.set(etree.QName(XLINK, "type"), "simple")
    sref.set(etree.QName(XLINK, "href"), schema_ref)

    context = etree.SubElement(root, etree.QName(XBRLI, "context"), id=ctx.context_id)
    entity = etree.SubElement(context, etree.QName(XBRLI, "entity"))
    ident = etree.SubElement(entity, etree.QName(XBRLI, "identifier"), scheme=ABN_SCHEME)
    ident.text = ctx.abn
    period = etree.SubElement(context, etree.QName(XBRLI, "period"))
    etree.SubElement(period, etree.QName(XBRLI, "startDate")).text = ctx.period_start.isoformat()
    etree.SubElement(period, etree.QName(XBRLI, "endDate")).text = ctx.period_end.isoformat()

    unit = etree.SubElement(root, etree.QName(XBRLI, "unit"), id=ctx.unit_id)
    etree.SubElement(unit, etree.QName(XBRLI, "measure")).text = "iso4217:AUD"

    for fact in facts:
        el = etree.SubElement(
            root,
            etree.QName(fact.concept_ns, fact.concept_name),
            contextRef=ctx.context_id,
            unitRef=ctx.unit_id,
            decimals=fact.decimals,
        )
        el.text = _money(fact.value)

    return etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", pretty_print=True
    )


def envelope_parts(document: bytes) -> tuple[str, str]:
    """Return ``(envelope_b64, envelope_sha256_hex)`` for the lodge-server body.

    Mirrors ``services/lodgement/remote.py:_envelope_payload`` so the hash and
    the base64 are always derived from the same bytes.
    """
    return (
        base64.b64encode(document).decode("ascii"),
        hashlib.sha256(document).hexdigest(),
    )
