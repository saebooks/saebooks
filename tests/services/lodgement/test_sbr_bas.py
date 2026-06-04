"""Unit tests for the SBR BAS XBRL generator (pure — no DB)."""
from __future__ import annotations

import base64
import hashlib
from datetime import date
from decimal import Decimal

from lxml import etree

from saebooks.services.lodgement.sbr import (
    BasFigures,
    ReportingContext,
    build_bas_document,
    envelope_parts,
)
from saebooks.services.lodgement.sbr.bas import _AS_CONCEPTS, AS_TAXONOMY_NS

XBRLI = "http://www.xbrl.org/2003/instance"


def _ctx() -> ReportingContext:
    return ReportingContext(
        abn="51824753556",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
    )


def _figures() -> BasFigures:
    # G1 includes GST; 1A = GST on sales; 1B = GST on purchases.
    return BasFigures(
        g1=Decimal("11000"),
        g2=Decimal("0"),
        g3=Decimal("500"),
        g10=Decimal("2200"),
        g11=Decimal("3300"),
        label_1a=Decimal("1000"),
        label_1b=Decimal("500"),
    )


def test_bas_document_is_wellformed_xbrl_with_abn_and_period():
    doc = build_bas_document(_figures(), _ctx())
    root = etree.fromstring(doc)
    assert root.tag == f"{{{XBRLI}}}xbrl"

    ident = root.find(f".//{{{XBRLI}}}identifier")
    assert ident is not None
    assert ident.get("scheme") == "http://www.ato.gov.au/abn"
    assert ident.text == "51824753556"

    assert root.find(f".//{{{XBRLI}}}startDate").text == "2026-01-01"
    assert root.find(f".//{{{XBRLI}}}endDate").text == "2026-03-31"

    unit = root.find(f"{{{XBRLI}}}unit")
    assert unit.find(f"{{{XBRLI}}}measure").text == "iso4217:AUD"


def test_bas_facts_match_figures_and_derive_net_9():
    doc = build_bas_document(_figures(), _ctx())
    root = etree.fromstring(doc)

    def fact(label: str) -> str:
        el = root.find(f"{{{AS_TAXONOMY_NS}}}{_AS_CONCEPTS[label]}")
        assert el is not None, f"missing fact for {label}"
        assert el.get("contextRef") == "ctx-period"
        assert el.get("unitRef") == "AUD"
        return el.text

    assert fact("G1") == "11000"
    assert fact("G3") == "500"
    assert fact("G10") == "2200"
    assert fact("G11") == "3300"
    assert fact("1A") == "1000"
    assert fact("1B") == "500"
    # Label 9 (net) = 1A - 1B = 500, derived not stored.
    assert fact("9") == "500"


def test_nil_labels_are_emitted_explicitly():
    doc = build_bas_document(_figures(), _ctx())
    root = etree.fromstring(doc)
    g2 = root.find(f"{{{AS_TAXONOMY_NS}}}{_AS_CONCEPTS['G2']}")
    assert g2 is not None and g2.text == "0"  # reported nil, not absent


def test_envelope_parts_hash_matches_document():
    doc = build_bas_document(_figures(), _ctx())
    b64, sha = envelope_parts(doc)
    assert base64.b64decode(b64) == doc
    assert sha == hashlib.sha256(doc).hexdigest()


def test_from_figures_json_tolerates_key_styles():
    # BASLine-style nested + mixed casing, as a tax_returns.figures JSONB might hold.
    figs = BasFigures.from_figures_json(
        {
            "G1": {"amount": "11000"},
            "g3": 500,
            "1A": "1000",
            "label_1b": 500,
            "G10": 2200,
            "G11": 3300,
        }
    )
    assert figs.g1 == Decimal("11000")
    assert figs.g3 == Decimal("500")
    assert figs.label_1a == Decimal("1000")
    assert figs.label_1b == Decimal("500")
    assert figs.net_9 == Decimal("500")
