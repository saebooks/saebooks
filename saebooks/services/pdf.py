"""PDF generation — invoices, bills, credit notes.

Uses ReportLab's Platypus flowable layout engine — pure-Python, no
system deps (Pango/Cairo/wkhtmltopdf), keeps the Docker image lean.

The public surface is deliberately small:

    render_invoice_pdf(ctx) -> bytes
    render_bill_pdf(ctx)    -> bytes
    render_credit_note_pdf(ctx) -> bytes

``ctx`` is a plain dict shaped in the router — the service layer never
touches the PDF module. That keeps invoice posting testable without a
reportlab install on CI and lets us swap the renderer later (WeasyPrint,
LaTeX via Overleaf-API, Typst) without churning callers.

Expected context shape (``render_invoice_pdf`` / ``render_bill_pdf``):

    {
        "kind": "TAX INVOICE" | "BILL" | "CREDIT NOTE",
        "number": "INV-000042",
        "issue_date": "2026-04-20",
        "due_date":   "2026-05-18",
        "company": {
            "name": "Sauer Pty Ltd ATF Saueesti Trust",
            "abn":  "87 744 586 592",
            "address_lines": ["...", "..."],
            "email": "accounts@sauer.com.au",
        },
        "contact": {
            "name": "Acme Corp",
            "abn":  "12 345 678 901",
            "address_lines": ["..."],
        },
        "lines": [
            {"description": "...", "quantity": "1", "unit_price": "100.00",
             "tax_label": "GST 10%", "line_total": "100.00"},
            ...
        ],
        "subtotal": "100.00",
        "tax_total": "10.00",
        "total":    "110.00",
        "amount_paid": "0.00",
        "balance_due": "110.00",
        "notes": "Thanks for your business",
        "payment_terms": "Net 28 days",
    }
"""
from __future__ import annotations

from io import BytesIO
from typing import Any

from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.lib.styles import (  # type: ignore[import-untyped]
    ParagraphStyle,
    getSampleStyleSheet,
)
from reportlab.lib.units import mm  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle(
            "h1",
            parent=base["Heading1"],
            fontSize=22,
            leading=26,
            spaceAfter=4,
            textColor=colors.HexColor("#0F172A"),
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontSize=10,
            leading=12,
            spaceAfter=2,
            textColor=colors.HexColor("#64748B"),
            fontName="Helvetica-Bold",
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontSize=9, leading=11
        ),
        "small": ParagraphStyle(
            "small",
            parent=base["Normal"],
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#64748B"),
        ),
        "number": ParagraphStyle(
            "number",
            parent=base["Normal"],
            fontSize=10,
            leading=12,
            alignment=2,  # right
        ),
        "right": ParagraphStyle(
            "right", parent=base["Normal"], fontSize=9, leading=11, alignment=2
        ),
    }


def _address_block(ctx_block: dict[str, Any], styles: dict[str, ParagraphStyle]) -> list[Any]:
    out: list[Any] = [Paragraph(ctx_block.get("name", ""), styles["body"])]
    if abn := ctx_block.get("abn"):
        out.append(Paragraph(f"ABN {abn}", styles["small"]))
    for line in ctx_block.get("address_lines") or []:
        if line:
            out.append(Paragraph(line, styles["small"]))
    if email := ctx_block.get("email"):
        out.append(Paragraph(email, styles["small"]))
    return out


def _line_items_table(ctx: dict[str, Any]) -> Table:
    rows: list[list[Any]] = [
        ["Description", "Qty", "Unit price", "Tax", "Amount"],
    ]
    for line in ctx.get("lines", []):
        rows.append(
            [
                line.get("description", ""),
                line.get("quantity", ""),
                line.get("unit_price", ""),
                line.get("tax_label", ""),
                line.get("line_total", ""),
            ]
        )
    col_widths = [95 * mm, 15 * mm, 25 * mm, 20 * mm, 25 * mm]
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return t


def _totals_block(ctx: dict[str, Any]) -> Table:
    rows: list[list[Any]] = [
        ["Subtotal", ctx.get("subtotal", "")],
        ["Tax", ctx.get("tax_total", "")],
        ["Total", ctx.get("total", "")],
    ]
    if ctx.get("amount_paid") not in (None, "", "0.00"):
        rows.append(["Paid", ctx.get("amount_paid", "")])
        rows.append(["Balance due", ctx.get("balance_due", "")])

    t = Table(rows, colWidths=[30 * mm, 30 * mm], hAlign="RIGHT")
    t.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
                ("LINEABOVE", (0, 2), (-1, 2), 0.5, colors.HexColor("#0F172A")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return t


def render_document_pdf(ctx: dict[str, Any]) -> bytes:
    """Generic renderer — invoice / bill / credit note share the same layout,
    only the top-left ``kind`` label changes."""
    styles = _styles()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"{ctx.get('kind', 'Document')} {ctx.get('number', '')}",
    )

    story: list[Any] = []

    # Header: kind + number on left, issue/due dates on right.
    header_rows = [
        [
            Paragraph(ctx.get("kind", "DOCUMENT").upper(), styles["h1"]),
            Paragraph(
                f"<b>Number</b> {ctx.get('number', '')}<br/>"
                f"<b>Issued</b> {ctx.get('issue_date', '')}<br/>"
                f"<b>Due</b> {ctx.get('due_date', '')}",
                styles["right"],
            ),
        ]
    ]
    header_tbl = Table(header_rows, colWidths=[95 * mm, 85 * mm])
    header_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(header_tbl)
    story.append(Spacer(1, 8 * mm))

    # Parties block — from / to side-by-side.
    from_col = [Paragraph("FROM", styles["h2"]), *_address_block(ctx.get("company", {}), styles)]
    to_col = [Paragraph("TO", styles["h2"]), *_address_block(ctx.get("contact", {}), styles)]
    parties = Table([[from_col, to_col]], colWidths=[90 * mm, 90 * mm])
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(parties)
    story.append(Spacer(1, 8 * mm))

    # Line items.
    story.append(_line_items_table(ctx))
    story.append(Spacer(1, 6 * mm))

    # Totals.
    story.append(_totals_block(ctx))
    story.append(Spacer(1, 8 * mm))

    if terms := ctx.get("payment_terms"):
        story.append(Paragraph("<b>Payment terms</b>", styles["h2"]))
        story.append(Paragraph(terms, styles["body"]))
        story.append(Spacer(1, 3 * mm))

    if notes := ctx.get("notes"):
        story.append(Paragraph("<b>Notes</b>", styles["h2"]))
        story.append(Paragraph(notes.replace("\n", "<br/>"), styles["body"]))

    doc.build(story)
    return buf.getvalue()


def render_invoice_pdf(ctx: dict[str, Any]) -> bytes:
    ctx = dict(ctx)
    ctx.setdefault("kind", "Tax Invoice")
    return render_document_pdf(ctx)


def render_bill_pdf(ctx: dict[str, Any]) -> bytes:
    ctx = dict(ctx)
    ctx.setdefault("kind", "Bill")
    return render_document_pdf(ctx)


def render_credit_note_pdf(ctx: dict[str, Any]) -> bytes:
    ctx = dict(ctx)
    ctx.setdefault("kind", "Credit Note")
    return render_document_pdf(ctx)
