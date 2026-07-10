"""Permission catalogue extension — finalized matrix (granular_permissions).

Extends the ``permissions`` table (50 codes seeded by 0033 + 0111) to
the full catalogue red-lined in
``~/records/saebooks/permission-matrix-draft.md`` (133 codes across 10
domain groups + ``dashboard.view`` = 134 total). This migration adds
the 84 codes marked NEW / NEW-gap in that draft; it never touches the
50 already-seeded rows.

Covers all four of Richard's catalogue decisions:

* **D3** — per-domain export codes (``report.export``,
  ``company.export`` [already seeded], ``audit.export`` [already
  seeded]) rather than one generic ``export.data``.
* **D4** — ``permission.manage`` split out of ``user.admin``.
* Missing full record types — ``expense.*`` and ``receipt.*`` (four
  codes each: view/create/post/void, matching the shape every other
  AR/AP record type already has).
* High-blast-radius admin codes — ``tax_code.manage``,
  ``bank_account.manage``.

Pure data migration — no schema change, no backfill, additive only.
Role grants for these new codes land in the NEXT migration
(``0194_role_permissions_rls``) alongside the D1-corrected
starter-role grid, once the ``roles`` table + role_id-keyed
``role_permissions`` schema is in place — a code with zero grant rows
is inert (nothing resolves it into anyone's permission set) which is
the deliberately safe order: catalogue exists before anything can be
granted against it.

Revision ID: 0192_permission_catalogue_extend
Revises:     0191_user_permission_tenant_rls
Create Date: 2026-07-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0192_permission_catalogue_extend"
down_revision: str | None = "0191_user_permission_tenant_rls"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


# (code, description) — the 84 NEW codes from the approved draft,
# grouped by the draft's 10 domain sections for readability. Codes
# already SEEDED by 0033/0111 are NOT repeated here.
NEW_PERMISSIONS: list[tuple[str, str]] = [
    # --- 1. Sales / Accounts Receivable ---------------------------- #
    ("contact.delete", "Archive (soft-delete) a contact"),
    ("contact.hard_delete", "Permanently purge a contact"),
    ("invoice.write_off", "Write off a bad debt on an invoice"),
    ("invoice.recovery", "Record a debt-recovery receipt against a written-off invoice"),
    ("invoice.send", "Email an invoice or reminder to the customer"),
    ("invoice.payment_link", "Generate a Stripe payment link for an invoice"),
    ("invoice.hard_delete", "Permanently purge a posted invoice"),
    ("quote.view", "View quotes"),
    ("quote.create", "Create and edit a draft quote"),
    ("quote.send", "Email a quote to a prospect"),
    ("quote.convert", "Convert an accepted quote to an invoice"),
    ("quote.delete", "Delete a quote"),
    ("credit_note.void", "Void a posted credit note"),
    ("recurring_invoice.manage", "Create, edit, pause, resume, or end a recurring invoice schedule"),
    ("recurring_invoice.run", "Trigger a recurring schedule to generate and post the next invoice"),
    ("one_off_customer.manage", "Create and edit a one-off (non-recurring) customer record"),
    ("attachment.upload", "Attach a file to any record"),
    ("attachment.delete", "Remove an attachment"),
    ("reclassification.create", "Propose moving a posted transaction to a different account"),
    ("reclassification.post", "Approve and post a reclassification"),
    # --- 2. Purchases / Accounts Payable ---------------------------- #
    ("bill.send", "Email a bill or remittance copy to the supplier"),
    ("purchase_order.view", "View purchase orders"),
    ("purchase_order.create", "Create and edit a draft purchase order"),
    ("purchase_order.approve", "Approve a purchase order before it can convert to a bill"),
    ("purchase_order.delete", "Delete a purchase order"),
    ("supplier_credit_note.view", "View supplier credit notes"),
    ("supplier_credit_note.create", "Create and edit a draft supplier credit note"),
    ("supplier_credit_note.post", "Post a supplier credit note"),
    ("supplier_credit_note.void", "Void a posted supplier credit note"),
    ("one_off_vendor.manage", "Create and edit a one-off vendor record"),
    ("expense.view", "View expenses"),
    ("expense.create", "Create and edit a draft expense"),
    ("expense.post", "Post an expense to the GL"),
    ("expense.void", "Void a posted expense"),
    ("receipt.view", "View receipts (reimbursement-style money-in record)"),
    ("receipt.create", "Create and edit a draft receipt"),
    ("receipt.post", "Post a receipt"),
    ("receipt.void", "Void a posted receipt"),
    ("document_inbox.upload", "Drop a document into the capture inbox"),
    ("document_inbox.review", "Edit or correct AI-extracted fields on a captured document"),
    ("document_inbox.publish", "Publish an inbox document as a real bill/expense/invoice record"),
    ("document_inbox.reject", "Reject or discard a captured document"),
    ("supplier_rule.manage", "Maintain inbox supplier auto-categorisation rules"),
    ("allocation_rule.manage", "Create and edit overhead allocation (split) rules"),
    ("allocation_rule.apply", "Apply an allocation rule to split a transaction"),
    ("statement.reconcile", "Supplier-statement reconciliation workflow"),
    # --- 3. Payments (spans AR and AP) ------------------------------ #
    ("payment.delete", "Delete a draft, unposted payment"),
    # --- 4. Banking --------------------------------------------------- #
    ("bank_account.manage", "Add, edit, or close a bank account record"),
    ("bank_rule.manage", "Create and edit bank auto-categorisation rules"),
    ("bank_statement_line.manage", "Manually add, edit, delete, or bulk-import statement lines"),
    ("reconciliation.match", "Match a bank statement line to an invoice or payment"),
    ("reconciliation.unmatch", "Undo a bank reconciliation match"),
    ("transfer.create", "Move money between two of your own bank accounts"),
    ("transfer.reverse", "Reverse a transfer"),
    ("cashbook.manage", "Cash-basis simplified entries"),
    # --- 5. Accounting / General Ledger ------------------------------ #
    ("account_range.manage", "Configure account-numbering ranges and prefixes"),
    ("journal_template.manage", "Create and edit reusable journal templates"),
    ("depreciation_model.manage", "Configure depreciation models (rates and methods)"),
    ("tax_code.manage", "Create and edit GST/tax codes and rates"),
    ("intercompany.post", "Post an intercompany transaction between related entities"),
    ("intercompany.reverse", "Reverse an intercompany posting"),
    ("branch.manage", "Configure multi-branch/location records"),
    # --- 6. Projects & Time Tracking ---------------------------------- #
    ("project.delete", "Archive or delete a project"),
    ("time_entry.create", "Log your own time"),
    ("time_entry.approve", "Approve or reject a timesheet before it converts to an invoice or pay run"),
    # --- 7. Payroll --------------------------------------------------- #
    ("pay_run.create", "Create a pay run and its lines"),
    ("pay_run.post", "Finalise and pay a pay run"),
    ("leave.manage", "View and adjust leave balances"),
    ("super_lodgement.create", "Prepare a super guarantee lodgement batch"),
    ("super_lodgement.finalise", "Finalise and mark a super batch as submitted"),
    ("tpar.create", "Prepare a Taxable Payments Annual Report batch"),
    ("tpar.finalise", "Finalise and lodge the TPAR"),
    # --- 8. Reports ------------------------------------------------- #
    ("report.export", "Export a report to CSV/PDF rather than view on-screen"),
    # --- 9. Compliance / Lodgement ------------------------------------ #
    ("bas.prepare", "Prepare or preview a BAS without lodging it"),
    ("tax_return.create", "Prepare a draft income tax return"),
    ("tax_return.lodge", "Lodge the income tax return"),
    ("ato_sbr.keystore.manage", "Manage the ATO SBR digital-certificate keystore"),
    ("ato_sbr.onboarding", "Run ATO SBR onboarding wizards"),
    # --- 10. Admin / System ------------------------------------------- #
    ("permission.manage", "Grant/revoke individual permission overrides and edit role grants"),
    ("audit.view", "View the audit log"),
    ("import.run", "Run a bulk data-import wizard"),
    ("api_token.manage", "Issue and revoke a personal API token"),
    ("principal_grant.manage", "Grant or revoke an external accountant's act-as access"),
    ("billing.manage", "Manage the tenant's SAE Books subscription/billing"),
]


def upgrade() -> None:
    conn = op.get_bind()
    for code, description in NEW_PERMISSIONS:
        conn.execute(
            sa.text(
                "INSERT INTO permissions (code, description) "
                "VALUES (:code, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"code": code, "description": description},
        )


def downgrade() -> None:
    conn = op.get_bind()
    codes = [code for code, _ in NEW_PERMISSIONS]
    conn.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"),
        {"codes": codes},
    )
