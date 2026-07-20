"""contacts: e-invoice recipient flag + Peppol participant id (M3, additive).

Estonia's Accounting Act e-invoicing rule, enacted 2025-07-01: a registered
"e-invoice recipient" business buyer may demand delivery as an e-invoice, and
the seller must comply within a statutory notice period. This is a per-buyer
(``Contact``) attribute, not a per-invoice one — the same buyer's demand
applies to every future invoice raised to them, so it belongs on the contact
record, surfaced at invoice-creation time (``services.einvoice.
buyer_requirement`` + its wiring into ``services.invoices.create_draft``/
``api_create`` — see that module).

Two additive, nullable-or-defaulted columns on ``contacts``:

- ``e_invoice_recipient`` — ``NOT NULL DEFAULT false``. Every existing
  contact (AU, NZ, UK, and pre-existing EE rows) defaults to ``false``,
  byte-identical behaviour to today. Only a contact explicitly flagged
  triggers the new-invoice surfacing.
- ``peppol_participant_id`` — nullable free text, the ``scheme:value`` wire
  form (``services.einvoice.operator.PeppolParticipantId.as_string()``, e.g.
  ``"0191:10137025"``) the operator transport addresses this buyer by on the
  Peppol network. NULL means "flagged as a recipient but no routing address
  on file yet" — a data-entry gap the review-note surfacing (services.
  invoices) exists partly to catch, not a migration-time backfill concern.

Both nullable-or-defaulted with no backfill required. Fully reversible via
``op.drop_column``.

Chains off the current company-DB single head ``0196_ee_filing_ref_cols``
(verified via ``alembic heads``). The suite pins ``len(get_heads()) == 1`` —
if a sibling packet also lands a migration off 0196, the orchestrator needs a
merge revision joining them.

Revision ID: 0197_contact_einvoice_recipient
Revises:     0196_ee_filing_ref_cols
Create Date: 2026-07-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0197_contact_einvoice_recipient"
down_revision: str | None = "0196_ee_filing_ref_cols"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "contacts"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "e_invoice_recipient",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "Buyer is a registered e-invoice recipient (EE Accounting Act, "
                "enacted 2025-07-01) — new invoices to this contact should be "
                "delivered as an EN 16931/Peppol BIS 3.0 e-invoice, not a plain "
                "PDF/email. Default false: byte-identical behaviour for every "
                "existing/non-EE contact."
            ),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "peppol_participant_id",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Peppol network routing address for this buyer, 'scheme:value' "
                "wire form (ISO 6523 EAS, e.g. '0191:10137025' for an Estonian "
                "registrikood) — see services.einvoice.operator."
                "PeppolParticipantId.as_string(). NULL = not yet on file."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "peppol_participant_id")
    op.drop_column(_TABLE, "e_invoice_recipient")
