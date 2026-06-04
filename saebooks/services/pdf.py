"""PDF generation — invoices, bills, credit notes, quotes.

Uses ReportLab's Platypus flowable layout engine — pure-Python, no
system deps (Pango/Cairo/wkhtmltopdf), keeps the Docker image lean.

The public surface is deliberately small:

    render_invoice_pdf(ctx) -> bytes
    render_bill_pdf(ctx)    -> bytes
    render_credit_note_pdf(ctx) -> bytes
    render_quote_pdf(ctx)   -> bytes

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


# ═══════════════════════════════════════════════════════════════════════════
# Quote renderer — engineering quotes / estimates
#
# Distinct from the generic invoice/bill/credit-note layout above. Reproduces
# the design used by SAE Engineering's Overleaf .tex template:
#   - Letterhead with SAE logo + right-aligned contact block
#   - "ESTIMATE" + saeblue/saeaccent title row
#   - ESTIMATE TO / ESTIMATE DETAILS / PROJECT DETAILS info boxes
#   - Multiple sections, each with its own heading + 6-col line-item table
#     (Item / Description / Material / Qty / Approx. Lengths / Drawing Ref)
#     + section subtotal row
#   - TOTAL ESTIMATE bar
#   - Key Terms / Insurance / Bank Details / Acceptance
#
# Expected ctx shape:
#
#     {
#         "number": "1019",
#         "title":  "Barron Valley Gymnastics Club Extension",
#         "scope":  "Supply and install of …",
#         "issue_date":  "2026-04-03",
#         "expiry_date": "2026-04-17",
#         "validity_days": 14,
#         "deposit_pct":   "50",
#         "subtotal": "279580.00",
#         "total":    "307538.00",
#         "customer": {
#             "name":    "HC Building & Construction",
#             "contact": "Will Gard, Project Manager",
#             "email":   "projects@hcbuilding.com.au",
#             "phone":   "07 4045 5722",
#             "mobile":  "0497 005 604",
#         },
#         "lines": [
#             {
#                 "line_no": 1,
#                 "description": "1 — Main Portal Columns (C1): …",
#                 "quantity": "8",
#                 "line_total": "0",
#                 "section_label": "Section 1 — Structural Steel …",
#                 "material":    "250UB37",
#                 "length_note": "6.2–6.4 m",
#                 "drawing_ref": "DD-A-20-01-5, K-12317 DD2 (S01, S03, S05)",
#             },
#             … (priced lines have line_total != 0 → rendered as a subtotal row)
#         ],
#     }
# ═══════════════════════════════════════════════════════════════════════════

from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402

_QUOTE_TOKENS = {
    "blue":   colors.HexColor("#1B3A5C"),  # saeblue
    "grey":   colors.HexColor("#4A4A4A"),  # saegrey
    "ltgrey": colors.HexColor("#F2F2F2"),  # saelightgrey
    "accent": colors.HexColor("#E8792B"),  # saeaccent
    "border": colors.HexColor("#CCCCCC"),  # saeborder
    "footer": colors.HexColor("#194291"),  # saefooter
}

_ASSETS_DIR = Path(__file__).parent.parent / "assets"
_FONTS_REGISTERED = False


def _register_quote_fonts() -> None:
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
    from reportlab.pdfbase.pdfmetrics import registerFontFamily  # type: ignore[import-untyped]
    from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]

    fonts_dir = _ASSETS_DIR / "fonts"
    pdfmetrics.registerFont(TTFont("Montserrat",          str(fonts_dir / "Montserrat-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Montserrat-Bold",     str(fonts_dir / "Montserrat-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("Montserrat-Medium",   str(fonts_dir / "Montserrat-Medium.ttf")))
    pdfmetrics.registerFont(TTFont("Montserrat-SemiBold", str(fonts_dir / "Montserrat-SemiBold.ttf")))
    registerFontFamily(
        "Montserrat",
        normal="Montserrat",
        bold="Montserrat-Bold",
        italic="Montserrat",
        boldItalic="Montserrat-Bold",
    )
    _FONTS_REGISTERED = True


def _quote_styles() -> dict[str, ParagraphStyle]:
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT  # type: ignore[import-untyped]
    base = getSampleStyleSheet()
    return {
        "letterhead_company": ParagraphStyle("lh_c", parent=base["Normal"], fontName="Montserrat-Bold", fontSize=11, leading=13, textColor=_QUOTE_TOKENS["blue"], alignment=TA_RIGHT),
        "letterhead_line":    ParagraphStyle("lh_l", parent=base["Normal"], fontName="Montserrat",      fontSize=9,  leading=11, textColor=_QUOTE_TOKENS["grey"], alignment=TA_RIGHT),
        "title":              ParagraphStyle("qt_t", parent=base["Normal"], fontName="Montserrat-Bold", fontSize=22, leading=26, textColor=_QUOTE_TOKENS["blue"]),
        "title_number":       ParagraphStyle("qt_n", parent=base["Normal"], fontName="Montserrat-Bold", fontSize=14, leading=17, textColor=_QUOTE_TOKENS["accent"], alignment=TA_RIGHT),
        "box_label":          ParagraphStyle("bl",   parent=base["Normal"], fontName="Montserrat-Bold", fontSize=9,  leading=11, textColor=_QUOTE_TOKENS["blue"]),
        "box_body":           ParagraphStyle("bb",   parent=base["Normal"], fontName="Montserrat",      fontSize=9,  leading=12, textColor=_QUOTE_TOKENS["grey"]),
        "box_body_bold":      ParagraphStyle("bbb",  parent=base["Normal"], fontName="Montserrat-Bold", fontSize=9,  leading=12, textColor=_QUOTE_TOKENS["grey"]),
        "section_heading":    ParagraphStyle("sh",   parent=base["Normal"], fontName="Montserrat-Bold", fontSize=12, leading=15, textColor=_QUOTE_TOKENS["blue"], spaceAfter=1),
        "table_header":       ParagraphStyle("th",   parent=base["Normal"], fontName="Montserrat-Bold", fontSize=8,  leading=10, textColor=colors.white),
        "table_cell":         ParagraphStyle("tc",   parent=base["Normal"], fontName="Montserrat",      fontSize=8,  leading=10, textColor=_QUOTE_TOKENS["grey"]),
        "table_cell_c":       ParagraphStyle("tcc",  parent=base["Normal"], fontName="Montserrat",      fontSize=8,  leading=10, textColor=_QUOTE_TOKENS["grey"], alignment=TA_CENTER),
        "subtotal_label":     ParagraphStyle("sl",   parent=base["Normal"], fontName="Montserrat-Bold", fontSize=10, leading=12, textColor=_QUOTE_TOKENS["blue"]),
        "subtotal_amount":    ParagraphStyle("sa",   parent=base["Normal"], fontName="Montserrat-Bold", fontSize=10, leading=12, textColor=_QUOTE_TOKENS["blue"], alignment=TA_RIGHT),
        "total_label":        ParagraphStyle("tl",   parent=base["Normal"], fontName="Montserrat-Bold", fontSize=14, leading=17, textColor=colors.white),
        "total_amount":       ParagraphStyle("ta",   parent=base["Normal"], fontName="Montserrat-Bold", fontSize=14, leading=17, textColor=colors.white, alignment=TA_RIGHT),
        "key_terms_body":     ParagraphStyle("ktb",  parent=base["Normal"], fontName="Montserrat",      fontSize=9,  leading=12, textColor=_QUOTE_TOKENS["grey"], spaceAfter=2),
        "key_terms_label":    ParagraphStyle("ktl",  parent=base["Normal"], fontName="Montserrat-Bold", fontSize=9,  leading=12, textColor=_QUOTE_TOKENS["grey"]),
    }


def _quote_page_background(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Montserrat", 8)
    canvas.setFillColor(_QUOTE_TOKENS["footer"])
    page_w, _ = A4
    canvas.drawCentredString(page_w / 2, 22 * mm, "Sauer Pty Ltd ACN 683 275 756")
    canvas.drawCentredString(page_w / 2, 18 * mm, "ATF")
    canvas.drawCentredString(page_w / 2, 14 * mm, "Saueesti Trust ABN 87 744 586 592")
    canvas.drawRightString(page_w - 20 * mm, 14 * mm, f"Page {canvas.getPageNumber()}")
    canvas.setStrokeColor(_QUOTE_TOKENS["footer"])
    canvas.setLineWidth(0.4)
    canvas.line(20 * mm, 28 * mm, page_w - 20 * mm, 28 * mm)
    canvas.restoreState()


def _fmt_money(amount: Any) -> str:
    d = Decimal(str(amount))
    if d == d.to_integral_value():
        return f"{int(d):,}"
    return f"{d:,.2f}"


def _fmt_qty(q: Any) -> str:
    if q is None or q == "":
        return ""
    try:
        d = Decimal(str(q))
        if d == d.to_integral_value():
            return str(int(d))
        return f"{d:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(q)


def _format_quote_date(iso: str) -> str:
    try:
        y, m, day = iso.split("-")
        months = ["January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"]
        return f"{int(day)} {months[int(m) - 1]} {y}"
    except Exception:
        return iso or "—"


def _clean_quote_description(desc: str) -> str:
    """Strip leading 'N — ' from saebooks description."""
    if " —" in desc:
        return desc.split(" —", 1)[1].strip()
    return desc


def _quote_letterhead(s: dict[str, ParagraphStyle]) -> Table:
    from reportlab.platypus import Image  # type: ignore[import-untyped]
    logo_path = _ASSETS_DIR / "sae-logo.png"
    img = Image(str(logo_path), width=70 * mm, height=70 * mm * 495 / 1200)
    contact = [
        Paragraph("SAE Engineering", s["letterhead_company"]),
        Spacer(1, 2),
        Paragraph("PO Box 592, Bungalow QLD 4870", s["letterhead_line"]),
        Paragraph("<b>P:</b> 07 4243 3488 &nbsp;&nbsp; <b>M:</b> 0457 704 373", s["letterhead_line"]),
        Paragraph("<b>E:</b> admin@saee.com.au", s["letterhead_line"]),
        Paragraph("<b>W:</b> saee.com.au", s["letterhead_line"]),
        Spacer(1, 2),
        Paragraph("ABN: 87 744 586 592 &nbsp;&nbsp; QBCC: 15231284", s["letterhead_line"]),
    ]
    t = Table([[img, contact]], colWidths=[70 * mm, 100 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _saeblue_rule_table() -> Table:
    t = Table([[""]], colWidths=[170 * mm], rowHeights=[2])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _QUOTE_TOKENS["blue"]),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _quote_title_row(quote_number: str, s: dict[str, ParagraphStyle]) -> Table:
    left = Paragraph("ESTIMATE", s["title"])
    right = Paragraph(f"SAE-2026-{quote_number}", s["title_number"])
    t = Table([[left, right]], colWidths=[90 * mm, 80 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _quote_info_boxes(ctx: dict[str, Any], s: dict[str, ParagraphStyle]) -> Table:
    customer = ctx.get("customer", {}) or {}
    number = f"SAE-2026-{ctx['number']}"
    estimate_to_block = [
        Paragraph("ESTIMATE TO:", s["box_label"]),
        Spacer(1, 4),
        Paragraph(f"<b>{customer.get('name', '')}</b>", s["box_body_bold"]),
    ]
    if customer.get("contact"):
        estimate_to_block.append(Paragraph(customer["contact"], s["box_body"]))
    if customer.get("phone") or customer.get("mobile"):
        parts = []
        if customer.get("phone"):
            parts.append(f"<b>P:</b> {customer['phone']}")
        if customer.get("mobile"):
            parts.append(f"<b>M:</b> {customer['mobile']}")
        estimate_to_block.append(Spacer(1, 3))
        estimate_to_block.append(Paragraph(" &nbsp;&nbsp; ".join(parts), s["box_body"]))
    if customer.get("email"):
        estimate_to_block.append(Paragraph(f"<b>E:</b> {customer['email']}", s["box_body"]))

    details_block = [
        Paragraph("ESTIMATE DETAILS:", s["box_label"]),
        Spacer(1, 4),
        Paragraph(f"<b>Estimate No:</b>&nbsp;&nbsp; {number}", s["box_body"]),
        Paragraph(f"<b>Date:</b>&nbsp;&nbsp; {_format_quote_date(ctx.get('issue_date', ''))}", s["box_body"]),
        Paragraph(
            f"<b>Valid Until:</b>&nbsp;&nbsp; {_format_quote_date(ctx['expiry_date']) if ctx.get('expiry_date') else '—'}",
            s["box_body"],
        ),
    ]
    t = Table([[estimate_to_block, details_block]], colWidths=[83 * mm, 83 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, 0), _QUOTE_TOKENS["ltgrey"]),
        ("BACKGROUND", (1, 0), (1, 0), _QUOTE_TOKENS["ltgrey"]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _quote_project_box(ctx: dict[str, Any], s: dict[str, ParagraphStyle]) -> Table:
    rows: list[Any] = [
        Paragraph("PROJECT DETAILS:", s["box_label"]),
        Spacer(1, 4),
        Paragraph(f"<b>Project:</b> {ctx.get('title') or '—'}", s["box_body"]),
    ]
    if scope := ctx.get("scope"):
        rows.append(Spacer(1, 2))
        rows.append(Paragraph(f"<b>Scope:</b> {scope}", s["box_body"]))
    t = Table([[rows]], colWidths=[170 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), _QUOTE_TOKENS["ltgrey"]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _quote_section_heading_block(text: str, s: dict[str, ParagraphStyle]) -> list:
    return [
        Paragraph(text, s["section_heading"]),
        Table([[""]], colWidths=[170 * mm], rowHeights=[0.4], style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _QUOTE_TOKENS["border"]),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ])),
        Spacer(1, 4),
    ]


def _quote_section_table(section_lines: list[dict], s: dict[str, ParagraphStyle]) -> list:
    """Render one section: 6-col table of descriptive lines + saelightgrey subtotal row(s)."""
    header = [
        Paragraph("Item", s["table_header"]),
        Paragraph("Description", s["table_header"]),
        Paragraph("Material", s["table_header"]),
        Paragraph("Qty", s["table_header"]),
        Paragraph("Approx. Lengths", s["table_header"]),
        Paragraph("Drawing Ref.", s["table_header"]),
    ]
    rows: list[list[Any]] = [header]
    style_cmds: list[Any] = [
        ("BACKGROUND", (0, 0), (-1, 0), _QUOTE_TOKENS["blue"]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, _QUOTE_TOKENS["border"]),
    ]

    descriptive = [ln for ln in section_lines if Decimal(str(ln.get("line_total", "0"))) == 0]
    priced = [ln for ln in section_lines if Decimal(str(ln.get("line_total", "0"))) != 0]

    for i, ln in enumerate(descriptive):
        desc_full = ln.get("description", "")
        num_str = desc_full.split(" —")[0] if " —" in desc_full else str(ln.get("line_no", ""))
        rows.append([
            Paragraph(num_str, s["table_cell_c"]),
            Paragraph(_clean_quote_description(desc_full), s["table_cell"]),
            Paragraph(ln.get("material") or "", s["table_cell"]),
            Paragraph(_fmt_qty(ln.get("quantity")), s["table_cell_c"]),
            Paragraph(ln.get("length_note") or "", s["table_cell"]),
            Paragraph(ln.get("drawing_ref") or "", s["table_cell"]),
        ])
        row_idx = len(rows) - 1
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, row_idx), (-1, row_idx), _QUOTE_TOKENS["ltgrey"]))

    col_widths = [10 * mm, 50 * mm, 35 * mm, 15 * mm, 25 * mm, 35 * mm]
    elements: list[Any] = []
    if descriptive:
        t = Table(rows, colWidths=col_widths, repeatRows=1)
        t.setStyle(TableStyle(style_cmds))
        elements.append(t)

    for ln in priced:
        label = ln.get("description") or "Subtotal"
        amount = _fmt_money(ln.get("line_total"))
        sub = Table(
            [[Paragraph(label, s["subtotal_label"]), Paragraph(f"${amount}", s["subtotal_amount"])]],
            colWidths=[130 * mm, 40 * mm],
        )
        sub.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _QUOTE_TOKENS["ltgrey"]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LINEABOVE", (0, 0), (-1, -1), 0.4, _QUOTE_TOKENS["border"]),
        ]))
        elements.append(sub)

    elements.append(Spacer(1, 4 * mm))
    return elements


def _quote_total_bar(total: Any, s: dict[str, ParagraphStyle]) -> Table:
    t = Table(
        [[Paragraph("TOTAL ESTIMATE (ex GST)", s["total_label"]),
          Paragraph(f"${_fmt_money(total)}", s["total_amount"])]],
        colWidths=[130 * mm, 40 * mm],
    )
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _QUOTE_TOKENS["blue"]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _quote_key_terms(ctx: dict[str, Any], s: dict[str, ParagraphStyle]) -> list:
    items = [
        f"<b>Validity:</b> {ctx.get('validity_days', 28)} days from date of estimate. Material pricing subject to change beyond this period.",
        f"<b>Payment Terms:</b> {Decimal(str(ctx.get('deposit_pct', '50'))).normalize()}% deposit on acceptance; balance per progress payment schedule.",
        "<b>Programme:</b> Approximate 8–10 week lead time from receipt of approved IFC drawings and deposit.",
        "<b>Late Fee:</b> 2.5% per month on overdue amounts.",
        "<b>PPSA:</b> SAE Engineering retains a security interest in all goods supplied until payment is received in full.",
        '<b>Full Terms:</b> <link href="https://saee.com.au/terms-of-trade" color="#1B3A5C">saee.com.au/terms-of-trade</link>',
    ]
    out = _quote_section_heading_block("Key Terms", s)
    for it in items:
        out.append(Paragraph(f"• {it}", s["key_terms_body"]))
    return out


def _quote_insurance(s: dict[str, ParagraphStyle]) -> list:
    out = _quote_section_heading_block("Insurance & Licences", s)
    rows = [
        ["Public Liability:", "$20,000,000 (valid to 23/12/2026)"],
        ["WorkCover:", "Policy WSM241037628 (valid to 30/06/2026)"],
        ["QBCC Licence:", "15231284"],
    ]
    t = Table(rows, colWidths=[40 * mm, 130 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Montserrat-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Montserrat"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (-1, -1), _QUOTE_TOKENS["grey"]),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    out.append(t)
    return out


def _quote_bank_box(quote_number: str, s: dict[str, ParagraphStyle]) -> Table:
    rows: list[Any] = [
        Paragraph("<b>Bank Details for EFT Payment:</b>", s["box_label"]),
        Spacer(1, 4),
        Paragraph("<b>Bank:</b> &nbsp;&nbsp; Westpac", s["box_body"]),
        Paragraph("<b>Account:</b> &nbsp;&nbsp; Sauer Pty Ltd", s["box_body"]),
        Paragraph("<b>BSB:</b> &nbsp;&nbsp; 034-193", s["box_body"]),
        Paragraph("<b>Account:</b> &nbsp;&nbsp; 485846", s["box_body"]),
        Paragraph(f"<b>Reference:</b> &nbsp;&nbsp; SAE-2026-{quote_number}", s["box_body"]),
    ]
    t = Table([[rows]], colWidths=[170 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _QUOTE_TOKENS["ltgrey"]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def render_quote_pdf(ctx: dict[str, Any]) -> bytes:
    """Render a SAE Engineering quote / estimate PDF.

    Designed to match the existing Overleaf .tex template. ctx shape documented
    at the top of this section.
    """
    _register_quote_fonts()
    s = _quote_styles()

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=15 * mm, bottomMargin=32 * mm,
        title=f"SAE-2026-{ctx['number']} — {ctx.get('title', '')}",
    )

    story: list[Any] = []
    story.append(_quote_letterhead(s))
    story.append(Spacer(1, 4 * mm))
    story.append(_saeblue_rule_table())
    story.append(Spacer(1, 4 * mm))
    story.append(_quote_title_row(ctx["number"], s))
    story.append(Spacer(1, 6 * mm))
    story.append(_quote_info_boxes(ctx, s))
    story.append(Spacer(1, 4 * mm))
    story.append(_quote_project_box(ctx, s))
    story.append(Spacer(1, 4 * mm))

    sections: dict[str, list] = {}
    for ln in ctx.get("lines", []):
        sec = ln.get("section_label") or "Other"
        sections.setdefault(sec, []).append(ln)

    for sec_label, sec_lines in sections.items():
        story.extend(_quote_section_heading_block(sec_label, s))
        story.extend(_quote_section_table(sec_lines, s))

    story.append(_quote_total_bar(ctx.get("subtotal", "0"), s))
    story.append(Spacer(1, 6 * mm))
    story.extend(_quote_key_terms(ctx, s))
    story.append(Spacer(1, 4 * mm))
    story.extend(_quote_insurance(s))
    story.append(Spacer(1, 4 * mm))
    story.append(_quote_bank_box(ctx["number"], s))
    story.append(Spacer(1, 4 * mm))
    story.extend(_quote_section_heading_block("Acceptance", s))
    story.append(Paragraph(
        f"To proceed with this estimate, please issue a purchase order referencing "
        f"<b>SAE-2026-{ctx['number']}</b> to "
        f'<link href="mailto:admin@saee.com.au" color="#1B3A5C">admin@saee.com.au</link>. '
        f"Upon receipt of your purchase order, we will issue a formal tax invoice for the deposit "
        f"amount and schedule the works accordingly.",
        s["key_terms_body"],
    ))
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("<b>Prepared by:</b>", s["key_terms_label"]))
    story.append(Spacer(1, 2))
    story.append(Paragraph("Richard Sauer", s["box_body"]))
    story.append(Paragraph("Director — SAE Engineering", s["box_body"]))
    story.append(Paragraph(
        '<link href="mailto:admin@saee.com.au" color="#1B3A5C">admin@saee.com.au</link> | 0457 704 373',
        s["box_body"],
    ))

    doc.build(story, onFirstPage=_quote_page_background, onLaterPages=_quote_page_background)
    return buf.getvalue()
