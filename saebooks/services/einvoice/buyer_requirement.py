"""Buyer-demand e-invoice surfacing (M3, task 3 — additive).

Estonia's Accounting Act e-invoicing rule, enacted **2025-07-01**: a business
registered as an e-invoice recipient may require its suppliers to deliver
invoices as e-invoices; the seller must comply within a statutory notice
period. ``Contact.e_invoice_recipient`` (migration 0197) is where a company
flags a buyer as having made that demand — a per-buyer attribute, since the
demand applies to every future invoice raised to them, not just one.

This module is the "surfacing" half (task 3's third ask): a small pure
predicate + a human-readable note, wired into ``services.invoices.
create_draft``/``api_create`` (see those functions) so a NEW invoice to a
flagged contact is automatically ``flagged_for_review`` with an explanatory
``review_note`` — reusing the EXISTING generic books-review mechanism
(``Invoice.flagged_for_review``/``review_note``, migration 0157) rather than
adding new Invoice-level state. This is engine-only, no UI: the surfacing IS
the review-queue flag + note, visible via ``list_active(flagged=True)`` and
any web/API client that already renders ``review_note`` for a flagged
invoice.

Deliberately NOT wired: automatically generating/sending the e-invoice at
create time. Whether the actual EN 16931 file has been produced and
delivered for THIS invoice is a workflow decision for the caller (the
generator in ``generator.py`` + the operator transport in ``operator.py``);
this module only makes the underlying buyer-side legal requirement visible
so it isn't missed.
"""
from __future__ import annotations

from saebooks.models.contact import Contact

_REQUIREMENT_NOTE_TEMPLATE = (
    "Buyer is a registered e-invoice recipient (EE Accounting Act, enacted "
    "2025-07-01) — deliver this invoice as an EN 16931/Peppol BIS 3.0 "
    "e-invoice, not a plain PDF/email."
)


def einvoice_required(contact: Contact) -> bool:
    """True when ``contact`` has been flagged as demanding e-invoice
    delivery. Pure predicate — no DB access, no side effect."""
    return bool(contact.e_invoice_recipient)


def describe_requirement(contact: Contact) -> str:
    """Human-readable note for a flagged contact — used as the new
    invoice's ``review_note`` (see module docstring). Appends the buyer's
    on-file Peppol routing address when present, or flags its absence (a
    common real gap: a buyer can be flagged as a recipient before their
    routing address is captured)."""
    note = _REQUIREMENT_NOTE_TEMPLATE
    if contact.peppol_participant_id:
        note += f" Peppol routing address on file: {contact.peppol_participant_id}."
    else:
        note += " No Peppol routing address on file yet — capture one before sending."
    return note


def review_note_for_new_invoice(contact: Contact | None) -> str | None:
    """The one call site ``services.invoices`` needs: ``None`` when no
    surfacing applies (contact missing, or not flagged), else the note to
    stamp onto the new ``Invoice.review_note`` alongside
    ``flagged_for_review=True``."""
    if contact is None or not einvoice_required(contact):
        return None
    return describe_requirement(contact)
