"""Smoke tests for the Batch RR2 classic theme extensions.

RR2 adds form overrides (invoices/bills/form + invoices/detail), the
in-page modal editing wiring, and a classic-grid treatment for the
aged-debtors report. These tests check the template source against
the keyboard + layout conventions the theme promises (F9 post binding,
F12 save-draft binding, master-detail shape, summary sidebar).

We don't render through the live app — the ChoiceLoader tests in
``tests/test_classic_theme.py`` already cover that. Here we verify the
structural properties that a reviewer would want to see without
booting Chrome.
"""
from __future__ import annotations

from pathlib import Path

import pytest

THEME_ROOT = (
    Path(__file__).resolve().parent.parent / "saebooks" / "templates" / "themes" / "classic"
)


# ---------------------------------------------------------------- #
# Form overrides
# ---------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relpath,action_prefix,contact_label",
    [
        ("invoices/form.html", "/invoices", "Customer"),
        ("bills/form.html", "/bills", "Supplier"),
    ],
)
def test_classic_form_has_master_detail_layout(
    relpath: str, action_prefix: str, contact_label: str
) -> None:
    body = (THEME_ROOT / relpath).read_text()
    # Master-detail CSS class lands on the outer grid.
    assert "master-detail" in body
    # Summary sidebar renders a <dl class="summary"> with status badge.
    assert 'dl class="summary"' in body
    # The customer/supplier label is present (picked it up from header).
    assert contact_label in body
    # POSTs at the right action prefix.
    assert action_prefix in body


@pytest.mark.parametrize(
    "relpath",
    [
        "invoices/form.html",
        "bills/form.html",
    ],
)
def test_classic_form_has_save_draft_binding(relpath: str) -> None:
    """F12 submits the first form matching #save-draft."""

    body = (THEME_ROOT / relpath).read_text()
    assert 'id="save-draft"' in body
    # CSS class lights up the dense-grid classic-form typography.
    assert 'class="classic-form"' in body
    # Line grid uses the classic-grid class so j/k row nav works.
    assert "classic-grid" in body


def test_classic_form_mentions_f12_hint() -> None:
    """Plan spec: form buttons carry an (F12) inline hint."""

    body = (THEME_ROOT / "invoices" / "form.html").read_text()
    assert "F12" in body


# ---------------------------------------------------------------- #
# Invoice detail — F9 post binding
# ---------------------------------------------------------------- #


def test_classic_invoice_detail_f9_post_form_id() -> None:
    body = (THEME_ROOT / "invoices" / "detail.html").read_text()
    # F9 looks for #post-action or [data-post-action]. Our form carries
    # the id so the shortcut works on a DRAFT invoice.
    assert 'id="post-action"' in body
    assert "/post" in body
    assert "F9" in body


def test_classic_invoice_detail_has_master_detail_and_summary() -> None:
    body = (THEME_ROOT / "invoices" / "detail.html").read_text()
    assert "master-detail" in body
    assert "detail-summary" in body
    # Totals block — the core of the classic summary sidebar.
    assert "Subtotal" in body
    assert "Total" in body


def test_classic_invoice_detail_lines_use_classic_grid() -> None:
    body = (THEME_ROOT / "invoices" / "detail.html").read_text()
    assert "classic-grid" in body


# ---------------------------------------------------------------- #
# Reports override — aged_ar
# ---------------------------------------------------------------- #


def test_classic_aged_ar_uses_classic_grid() -> None:
    body = (THEME_ROOT / "reports" / "aged_ar.html").read_text()
    assert "classic-grid" in body
    # Filter form promoted to classic toolbar.
    assert 'class="toolbar"' in body
    # Download CSV link preserved.
    assert "format=csv" in body


def test_classic_aged_ar_preserves_bucket_columns() -> None:
    body = (THEME_ROOT / "reports" / "aged_ar.html").read_text()
    # The five aging buckets are rendered via bucket_keys so we just
    # check the loop is still there.
    assert "bucket_keys" in body
    assert "grand_total" in body


# ---------------------------------------------------------------- #
# Modal editing — base.html HTMX wiring
# ---------------------------------------------------------------- #


def test_classic_base_opens_modal_on_htmx_swap() -> None:
    """base.html wires htmx:afterSwap → modal.showModal() so list pages
    can hx-target="#classic-modal" and get in-page editing for free."""

    body = (THEME_ROOT / "base.html").read_text()
    assert "htmx:afterSwap" in body
    assert "classic-modal" in body
    assert "showModal()" in body


def test_classic_base_modal_closeable_via_esc() -> None:
    """Pressing Esc while the modal is open closes it without
    affecting the palette."""

    body = (THEME_ROOT / "base.html").read_text()
    # Rely on the Esc branch checking modal.open before closing.
    assert "modal && modal.open" in body
