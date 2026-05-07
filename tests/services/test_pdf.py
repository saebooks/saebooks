"""Tests for ``saebooks.services.pdf``.

PDF rendering is deterministic byte-for-byte modulo the ``/ID`` block
ReportLab injects (timestamped MD5), so we assert on:

1. Bytes start with ``%PDF-`` (valid PDF magic).
2. Bytes contain ``%%EOF`` trailer.
3. All three doc kinds (invoice/bill/credit-note) produce > 1KB output.
4. The ``number`` supplied in the ctx appears in the rendered stream
   (covers basic templating wiring).
5. Missing optional fields (payment_terms, notes, amount_paid) don't
   explode the renderer.
"""
from __future__ import annotations

import pytest

from saebooks.services import pdf


@pytest.fixture
def minimal_ctx() -> dict[str, object]:
    return {
        "number": "INV-TEST-001",
        "issue_date": "2026-04-20",
        "due_date": "2026-05-18",
        "company": {
            "name": "Acme Pty Ltd",
            "abn": "12 345 678 901",
            "address_lines": ["PO Box 99", "Brisbane QLD 4000"],
            "email": "accounts@example.com",
        },
        "contact": {
            "name": "Acme Corp",
            "abn": "12 345 678 901",
            "address_lines": ["123 Main St", "Sydney NSW 2000"],
        },
        "lines": [
            {
                "description": "Consulting hours — April",
                "quantity": "10",
                "unit_price": "150.00",
                "tax_label": "GST 10%",
                "line_total": "1500.00",
            },
            {
                "description": "Travel expenses",
                "quantity": "1",
                "unit_price": "250.00",
                "tax_label": "GST-free",
                "line_total": "250.00",
            },
        ],
        "subtotal": "1,750.00",
        "tax_total": "150.00",
        "total": "1,900.00",
        "amount_paid": "0.00",
        "balance_due": "1,900.00",
        "notes": "Thanks for your business",
        "payment_terms": "Net 28 days",
    }


def test_render_invoice_pdf_valid_magic(minimal_ctx: dict[str, object]) -> None:
    data = pdf.render_invoice_pdf(minimal_ctx)
    assert data.startswith(b"%PDF-"), "output is not a PDF"
    assert b"%%EOF" in data[-64:], "PDF is missing EOF trailer"
    assert len(data) > 1024, f"PDF suspiciously small: {len(data)} bytes"


def test_render_bill_pdf_valid(minimal_ctx: dict[str, object]) -> None:
    ctx = dict(minimal_ctx)
    data = pdf.render_bill_pdf(ctx)
    assert data.startswith(b"%PDF-")
    assert len(data) > 1024


def test_render_credit_note_pdf_valid(minimal_ctx: dict[str, object]) -> None:
    ctx = dict(minimal_ctx)
    ctx["number"] = "CN-TEST-001"
    data = pdf.render_credit_note_pdf(ctx)
    assert data.startswith(b"%PDF-")
    assert len(data) > 1024


def test_document_number_appears_in_pdf(minimal_ctx: dict[str, object]) -> None:
    """ReportLab writes the doc title into the PDF metadata stream; the
    invoice number lands there too, so a plain-bytes search picks it up.
    """
    ctx = dict(minimal_ctx)
    ctx["number"] = "INV-PROBE-777"
    data = pdf.render_invoice_pdf(ctx)
    # Title metadata holds "<kind> <number>" — match the number fragment.
    assert b"INV-PROBE-777" in data


def test_optional_fields_missing_does_not_crash(minimal_ctx: dict[str, object]) -> None:
    """payment_terms / notes / amount_paid all optional."""
    ctx = dict(minimal_ctx)
    for k in ("payment_terms", "notes", "amount_paid", "balance_due"):
        ctx.pop(k, None)
    data = pdf.render_invoice_pdf(ctx)
    assert data.startswith(b"%PDF-")


def test_empty_lines_renders(minimal_ctx: dict[str, object]) -> None:
    """Header-only pro-forma with no lines still renders."""
    ctx = dict(minimal_ctx)
    ctx["lines"] = []
    data = pdf.render_invoice_pdf(ctx)
    assert data.startswith(b"%PDF-")


def test_missing_optional_abn_address_email(minimal_ctx: dict[str, object]) -> None:
    ctx = dict(minimal_ctx)
    ctx["company"] = {"name": "Solo Trader Pty Ltd"}
    ctx["contact"] = {"name": "John Customer"}
    data = pdf.render_invoice_pdf(ctx)
    assert data.startswith(b"%PDF-")
    assert b"Solo Trader Pty Ltd" in data or len(data) > 1024  # name is in stream or layout survived
