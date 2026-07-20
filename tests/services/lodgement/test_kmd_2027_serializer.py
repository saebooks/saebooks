"""2027 data-based KMD (XBRL GL EE0203001) serializer — golden + conformance.

Mirrors ``test_kmd_inf_golden.py``'s discipline but WITHOUT a DB: the serializer
is pure, so the canonical listing is hand-built (a representative subset of the
official package sample's 20 transactions, exercising every serializer branch)
and round-tripped through the real serializer, byte-compared to the committed
golden and structurally conformance-checked against the real package sample.

This file does STRUCTURAL conformance + a byte-for-byte golden. Full XSD
validation of the serializer output is done SEPARATELY in
``test_kmd_2027_schema_validation.py`` (which commits the four generic xbrl.org
base schemas the taxonomy imports by absolute URL and catalog-resolves them, so
``gl-plt-2026-03-31.xsd`` — which raises ``XMLSchemaParseError`` standalone —
loads and validates offline). Here we assert every emitted element/namespace is
one the official sample uses, plus the load-bearing tokens (``entryNumber``
EE0203001, ``accountSubType`` KMDTYYP2026ap).

No DB — pure dataclasses in, bytes out.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from lxml import etree

from saebooks.services.lodgement.kmd_2027 import mapping as m
from saebooks.services.lodgement.kmd_2027.serializer import (
    Kmd2027Listing,
    Kmd2027ReportingContext,
    Kmd2027Row,
    build_kmd_2027_xml_document,
)

_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "kmd_2027"
_SAMPLE = (
    Path(__file__).parent.parent.parent
    / "fixtures" / "emta_schemas" / "XBRL_GL_sample_20260617.xml"
)

_D = Decimal


def _maybe_regen(path: Path, data: bytes) -> None:
    if os.environ.get("SAEBOOKS_REGEN_FIXTURES"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _canonical_ctx() -> Kmd2027ReportingContext:
    return Kmd2027ReportingContext(
        regcode="10001234",
        period_start=date(2027, 1, 1),
        period_end=date(2027, 1, 31),
        creator_name="Testettevõte OÜ",
        creation_datetime=datetime(2027, 2, 8, 9, 36, 51, 554000),
        unique_id="10001234-2027-02-08T09:36:51.554",
        source_application="Raamatupidamistarkvara Test",
        entry_source_id="ERP-01",
        entry_source_count=2,
        period_extra_identifier=m.PERIOD_EXTRA_BANKRUPTCY,
    )


def _canonical_listing() -> Kmd2027Listing:
    """Ten rows chosen to exercise every serializer branch: standard supply
    with full partner code + measurable; prepayment (documentApplyToNumber);
    natural person (category 200); under-€1,000 (category 103); intra-Community
    supply with the country accountSub dimension; exempt supply (no taxes);
    signed credit invoice with two ALGSE_ARVE_KP original dates; reverse-charge
    acquisition (S_); input VAT (O_); and a negative correction (O_601)."""
    rows = [
        Kmd2027Row(1, "M_101", _D("2400"), _D("0.24"), "11111111",
                   m.IDENT_DESC_REGCODE, m.IDENT_CAT_STANDARD, "MA10001",
                   None, date(2027, 1, 1), _D("2400")),
        Kmd2027Row(2, "M_101", _D("2500"), _D("0.24"), "11111114",
                   m.IDENT_DESC_REGCODE, m.IDENT_CAT_STANDARD, None,
                   "EA10001", date(2027, 1, 2), _D("3000")),
        Kmd2027Row(3, "M_101", _D("1000"), _D("0.24"), None, None,
                   m.IDENT_CAT_NATURAL_PERSON),
        Kmd2027Row(4, "M_101", _D("175"), _D("0.24"), None, None,
                   m.IDENT_CAT_UNDER_THRESHOLD),
        Kmd2027Row(5, "M_201", _D("1200"), _D("0.00"), "FI08611111",
                   m.IDENT_DESC_VAT_NUMBER, m.IDENT_CAT_STANDARD, None, None,
                   None, None, (), "FI"),
        Kmd2027Row(6, "M_301", _D("875"), None),
        Kmd2027Row(7, "M_101", _D("-1000"), _D("0.24"), "11111131",
                   m.IDENT_DESC_REGCODE, m.IDENT_CAT_STANDARD, "KR0001", None,
                   date(2027, 1, 31), _D("-1000"),
                   (date(2025, 9, 27), date(2025, 10, 15))),
        Kmd2027Row(8, "S_101", _D("2500"), _D("0.24")),
        Kmd2027Row(9, "O_101", _D("240"), _D("0.24"), "13000001",
                   m.IDENT_DESC_REGCODE, m.IDENT_CAT_STANDARD, "OA27001", None,
                   date(2027, 1, 5), _D("1000")),
        Kmd2027Row(10, "O_601", _D("-240"), _D("0.24")),
    ]
    return Kmd2027Listing("10001234", date(2027, 1, 1), date(2027, 1, 31), rows)


def _element_qnames(root: etree._Element) -> set[str]:
    return {el.tag for el in root.iter() if isinstance(el.tag, str)}


def test_kmd_2027_golden_serialises_byte_for_byte() -> None:
    out = build_kmd_2027_xml_document(_canonical_listing(), _canonical_ctx())
    _maybe_regen(_FIXTURES / "golden.xml", out)
    assert out == (_FIXTURES / "golden.xml").read_bytes()


def test_kmd_2027_structurally_conforms_to_official_sample() -> None:
    """Every element/namespace we emit is one the real package sample uses;
    no invented names survive."""
    out = build_kmd_2027_xml_document(_canonical_listing(), _canonical_ctx())
    ours = etree.fromstring(out)
    sample = etree.parse(str(_SAMPLE)).getroot()

    extra = _element_qnames(ours) - _element_qnames(sample)
    assert not extra, f"emitted element(s) not in the official sample: {sorted(extra)}"

    # Namespace map matches the sample exactly (prefix + URI).
    assert dict(ours.nsmap) == dict(sample.nsmap)


def test_kmd_2027_carries_load_bearing_tokens() -> None:
    out = build_kmd_2027_xml_document(_canonical_listing(), _canonical_ctx())
    root = etree.fromstring(out)
    ns = m.NS_GL_COR
    assert root.tag == m.EL_XBRL
    assert root.find(f".//{{{ns}}}entryNumber").text == "EE0203001"
    subtypes = {e.text for e in root.iter(f"{{{ns}}}accountSubType")}
    assert "KMDTYYP2026ap" in subtypes
    # The intra-Community row carried the country dimension.
    assert "RTK2T2013ap" in subtypes
    codes = [e.text for e in root.iter(f"{{{ns}}}accountSubID")]
    assert "M_101" in codes and "S_101" in codes and "O_101" in codes


def test_official_sample_parses() -> None:
    root = etree.parse(str(_SAMPLE)).getroot()
    assert root.tag == m.EL_XBRL
    assert root.find(f".//{{{m.NS_GL_COR}}}entryNumber").text == "EE0203001"


def test_zero_period_emits_header_only() -> None:
    """A zero period ships the entryHeader with no entryDetail (GUIDE p.25)."""
    listing = Kmd2027Listing("10001234", date(2027, 1, 1), date(2027, 1, 31), [])
    root = etree.fromstring(build_kmd_2027_xml_document(listing, _canonical_ctx()))
    ns = m.NS_GL_COR
    header = root.find(f".//{{{ns}}}entryHeader")
    assert header is not None
    assert header.findall(f"{{{ns}}}entryDetail") == []
    assert header.find(f"{{{ns}}}entryNumber").text == "EE0203001"
