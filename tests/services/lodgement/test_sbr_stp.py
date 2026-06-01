"""Unit tests for the SBR STP2 PAYEVNT generator (pure — no DB)."""
from __future__ import annotations

from lxml import etree

from saebooks.services.lodgement.sbr import (
    build_stp_pay_event_document,
    envelope_parts,
)
from saebooks.services.lodgement.sbr.stp import (
    PAYEVNT_TAXONOMY_NS,
    _PAYEVNT_CONCEPTS,
    _PAYEVNTEMP_CONCEPTS,
)

XBRLI = "http://www.xbrl.org/2003/instance"


def _payload() -> dict:
    """A 2-payee PAY event, mirroring services/stp.py:build_pay_event output."""
    return {
        "schema_version": "STP2-1.0",
        "submission_software": {"name": "SAE Books", "version": "v2026.05"},
        "employer": {"abn": "51824753556", "legal_name": "Sauer Pty Ltd", "branch_code": "001"},
        "report_period": {"start": "2026-04-01", "end": "2026-04-30", "payment_date": "2026-04-30"},
        "event_type": "PAY",
        "payees": [
            {
                "payee_id_bms": "EMP-001",
                "tfn_status": "provided",
                "name": "Jane Citizen",
                "dob": "1990-02-03",
                "employment_basis": "F",
                "tax_treatment_code": "RTRTSX",
                "income_stream_type": "SAW",
                "ytd": {"gross": "52000.00", "tax": "9000.00", "super": "5980.00"},
                "period": {"gross": "4000.00", "tax": "700.00", "super": "460.00"},
                "super_fund": {"usi": "ABC0001AU", "member_number": "M-77"},
            },
            {
                "payee_id_bms": "EMP-002",
                "tfn_status": "provided",
                "name": "John Worker",
                "dob": "1985-11-20",
                "employment_basis": "P",
                "tax_treatment_code": "RTRTSX",
                "income_stream_type": "SAW",
                "ytd": {"gross": "26000.00", "tax": "3000.00", "super": "2990.00"},
                "period": {"gross": "2000.00", "tax": "230.00", "super": "230.00"},
                "super_fund": {"usi": "XYZ0002AU", "member_number": "M-88"},
            },
        ],
        "totals": {"gross": "78000.00", "tax": "12000.00", "super": "8970.00", "payee_count": 2},
    }


def _root():
    return etree.fromstring(build_stp_pay_event_document(_payload()))


def test_employer_context_carries_abn_and_period():
    root = _root()
    payer = root.find(f"{{{XBRLI}}}context[@id='ctx-payer']")
    assert payer is not None
    assert payer.find(f".//{{{XBRLI}}}identifier").text == "51824753556"
    assert payer.find(f".//{{{XBRLI}}}startDate").text == "2026-04-01"
    assert payer.find(f".//{{{XBRLI}}}endDate").text == "2026-04-30"


def test_one_context_per_payee_plus_employer():
    root = _root()
    ctx_ids = {c.get("id") for c in root.findall(f"{{{XBRLI}}}context")}
    assert ctx_ids == {"ctx-payer", "ctx-payee-1", "ctx-payee-2"}


def test_employer_totals_are_monetary_with_unit_and_cents():
    root = _root()
    gross = root.find(f"{{{PAYEVNT_TAXONOMY_NS}}}{_PAYEVNT_CONCEPTS['total_gross']}")
    assert gross is not None
    assert gross.get("contextRef") == "ctx-payer"
    assert gross.get("unitRef") == "AUD"
    assert gross.text == "78000.00"  # cents preserved (unlike whole-dollar BAS)


def test_payee_ytd_facts_under_their_own_context():
    root = _root()
    c = _PAYEVNTEMP_CONCEPTS
    # Jane's YTD gross under ctx-payee-1
    facts = root.findall(f"{{{PAYEVNT_TAXONOMY_NS}}}{c['ytd_gross']}")
    by_ctx = {f.get("contextRef"): f.text for f in facts}
    assert by_ctx["ctx-payee-1"] == "52000.00"
    assert by_ctx["ctx-payee-2"] == "26000.00"


def test_non_monetary_payee_facts_have_no_unit():
    root = _root()
    name = root.find(f"{{{PAYEVNT_TAXONOMY_NS}}}{_PAYEVNTEMP_CONCEPTS['name']}")
    assert name is not None
    assert name.get("unitRef") is None  # XBRL forbids a unit on non-numeric items
    assert name.text == "Jane Citizen"
    tfn = root.find(f"{{{PAYEVNT_TAXONOMY_NS}}}{_PAYEVNTEMP_CONCEPTS['tfn_status']}")
    assert tfn.text == "provided"  # status only — never the plaintext TFN


def test_envelope_parts_roundtrip():
    import base64
    import hashlib

    doc = build_stp_pay_event_document(_payload())
    b64, sha = envelope_parts(doc)
    assert base64.b64decode(b64) == doc
    assert sha == hashlib.sha256(doc).hexdigest()


def test_missing_optional_fields_are_skipped_not_emitted_empty():
    payload = _payload()
    payload["payees"][0]["super_fund"] = None  # no fund this period
    root = etree.fromstring(build_stp_pay_event_document(payload))
    usi = root.findall(f"{{{PAYEVNT_TAXONOMY_NS}}}{_PAYEVNTEMP_CONCEPTS['super_usi']}")
    # only payee-2 has a fund now
    assert {f.get("contextRef") for f in usi} == {"ctx-payee-2"}
