"""AU BAS summary export — CSV/XLSX row builder.

The BAS is an Australia-specific document, so its export column mapping lives
in the AU jurisdiction module rather than the neutral report-export core. The
neutral serializers (``services.report_exports.build_csv`` / ``build_xlsx``)
turn the ``(headers, rows, money_cols, text_cols)`` tuple this builder returns
into the actual file bytes — they know nothing about BAS labels.

One row per BAS label in ATO order, plus the derived 1A/1B/net-GST summary
rows. Amounts flow through the neutral money handling (exact 2-dp Decimal).
"""
from __future__ import annotations

from typing import Any

from saebooks.api.v1.schemas import BASSummary


def bas_summary_rows(
    report: BASSummary,
) -> tuple[list[str], list[list[Any]], list[int], list[int]]:
    """Return ``(headers, rows, money_cols, text_cols)`` for a BAS summary export.

    Columns: ``label, description, amount``. ``label`` is the ATO field code
    (G1, G2, …, 1A, 1B, net_gst); ``description`` is the ATO nomenclature.
    """
    headers = ["label", "description", "amount"]
    rows: list[list[Any]] = [
        ["G1", "Total sales (inc. GST)", report.g1_total_sales],
        ["G2", "Export sales", report.g2_export_sales],
        ["G3", "Other GST-free sales", report.g3_other_gst_free_sales],
        ["G10", "Capital acquisitions (inc. GST)", report.g10_capital_acquisitions],
        ["G11", "Non-capital acquisitions", report.g11_other_acquisitions],
        ["1A", "GST on sales", report.label_1a_gst_on_sales],
        ["1B", "GST on purchases", report.label_1b_gst_on_purchases],
        ["net_gst", f"Net GST ({report.remit_or_refund})", report.net_gst],
    ]
    if report.registration_effective_date is not None:
        rows.extend(
            [
                [
                    "G1_pre",
                    f"Pre-registration sales (before {report.registration_effective_date.isoformat()})",
                    report.g1_pre_registration,
                ],
                ["G1_post", "Post-registration sales", report.g1_post_registration],
            ]
        )
    # money col = amount (index 2); no free-text columns (labels/descriptions
    # are engine-controlled, not user input).
    return headers, rows, [2], []
