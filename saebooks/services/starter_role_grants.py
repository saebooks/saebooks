"""The finalized-matrix starter-role grant grid — LIVING copy.

This is the runtime source of truth ``services.roles.ensure_starter_roles``
seeds new/self-healed tenants from. It is a deliberate DUPLICATE of the
frozen data embedded in migration ``0194_role_permissions_rls``
(``_GRANTS`` there) — NOT an import from it. Migrations in this
codebase are frozen historical snapshots (0033/0058/0111 all embed
their own seed data inline rather than importing a services module,
specifically so replaying an old migration on a fresh install
reproduces exactly what shipped at that point in history, immune to
later refactors of this file). This module is the opposite: a LIVING
source, read on every permission resolution via
``services.roles.ensure_starter_roles``, that's allowed to evolve.

If the starter grid is ever revised again, update BOTH: this module
(so new/self-healed tenants get the new grants) and a NEW forward-only
migration that re-seeds existing tenants (never hand-edit 0194 after
it has shipped). This is the same "seed-then-diverge" trade-off every
other migration-seeded table in this codebase already lives with — not
new to this module.

D1 (bookkeeper is draft-only; Approver posts/voids/lodges) and D5
(Owner and Admin are identical grants, including ``billing.manage`` —
Richard explicitly rejected the draft's owner-exclusive proposal) are
both baked into this table. See ``permission-matrix-draft.md`` for the
per-code rationale and ``models/role.py``'s ``STARTER_ROLES`` for the
six role names/base_role mapping this keys off.
"""
from __future__ import annotations

# (code, owner_admin, bookkeeper, approver, readonly, payroll) — see
# migration 0194's module docstring for the full per-domain provenance.
# Kept byte-identical to that migration's `_GRANTS` as of 2026-07-10;
# see module docstring for why these are two independent copies.
GRANTS: tuple[tuple[str, bool, bool, bool, bool, bool], ...] = (
    ("dashboard.view", True, True, True, True, True),
    ("contact.view", True, True, True, True, False),
    ("contact.edit", True, True, True, False, False),
    ("contact.delete", True, False, True, False, False),
    ("contact.hard_delete", True, False, False, False, False),
    ("invoice.view", True, True, True, True, False),
    ("invoice.create", True, True, True, False, False),
    ("invoice.post", True, False, True, False, False),
    ("invoice.void", True, False, True, False, False),
    ("invoice.write_off", True, False, True, False, False),
    ("invoice.recovery", True, False, True, False, False),
    ("invoice.send", True, True, True, False, False),
    ("invoice.payment_link", True, True, True, False, False),
    ("invoice.hard_delete", True, False, False, False, False),
    ("quote.view", True, True, True, True, False),
    ("quote.create", True, True, True, False, False),
    ("quote.send", True, True, True, False, False),
    ("quote.convert", True, False, True, False, False),
    ("quote.delete", True, False, True, False, False),
    ("credit_note.view", True, True, True, True, False),
    ("credit_note.create", True, True, True, False, False),
    ("credit_note.post", True, False, True, False, False),
    ("credit_note.void", True, False, True, False, False),
    ("recurring_invoice.manage", True, True, True, False, False),
    ("recurring_invoice.run", True, False, True, False, False),
    ("one_off_customer.manage", True, True, True, False, False),
    ("item.view", True, True, True, True, False),
    ("item.edit", True, True, True, False, False),
    ("attachment.upload", True, True, True, False, False),
    ("attachment.delete", True, False, True, False, False),
    ("reclassification.create", True, True, True, False, False),
    ("reclassification.post", True, False, True, False, False),
    ("bill.view", True, True, True, True, False),
    ("bill.create", True, True, True, False, False),
    ("bill.post", True, False, True, False, False),
    ("bill.void", True, False, True, False, False),
    ("bill.send", True, True, True, False, False),
    ("purchase_order.view", True, True, True, True, False),
    ("purchase_order.create", True, True, True, False, False),
    ("purchase_order.approve", True, False, True, False, False),
    ("purchase_order.delete", True, False, True, False, False),
    ("supplier_credit_note.view", True, True, True, True, False),
    ("supplier_credit_note.create", True, True, True, False, False),
    ("supplier_credit_note.post", True, False, True, False, False),
    ("supplier_credit_note.void", True, False, True, False, False),
    ("one_off_vendor.manage", True, True, True, False, False),
    ("expense.view", True, True, True, True, False),
    ("expense.create", True, True, True, False, False),
    ("expense.post", True, False, True, False, False),
    ("expense.void", True, False, True, False, False),
    ("receipt.view", True, True, True, True, False),
    ("receipt.create", True, True, True, False, False),
    ("receipt.post", True, False, True, False, False),
    ("receipt.void", True, False, True, False, False),
    ("document_inbox.upload", True, True, True, False, False),
    ("document_inbox.review", True, True, True, False, False),
    ("document_inbox.publish", True, False, True, False, False),
    ("document_inbox.reject", True, True, True, False, False),
    ("supplier_rule.manage", True, True, True, False, False),
    ("allocation_rule.manage", True, True, True, False, False),
    ("allocation_rule.apply", True, False, True, False, False),
    ("statement.reconcile", True, True, True, False, False),
    ("payment.view", True, True, True, True, False),
    ("payment.create", True, True, True, False, False),
    ("payment.post", True, False, True, False, False),
    ("payment.delete", True, True, True, False, False),
    ("payment.void", True, False, True, False, False),
    ("bank.view", True, True, True, True, False),
    ("bank.sync", True, True, True, False, False),
    ("bank_account.manage", True, False, False, False, False),
    ("bank_rule.manage", True, True, True, False, False),
    ("bank_statement_line.manage", True, True, True, False, False),
    ("reconciliation.match", True, True, True, False, False),
    ("reconciliation.unmatch", True, False, True, False, False),
    ("transfer.create", True, False, True, False, False),
    ("transfer.reverse", True, False, True, False, False),
    ("cashbook.manage", True, True, True, False, False),
    ("account.view", True, True, True, True, False),
    ("account.edit", True, True, True, False, False),
    ("account_range.manage", True, False, False, False, False),
    ("journal.view", True, True, True, True, False),
    ("journal.draft", True, True, True, False, False),
    ("journal.post", True, False, True, False, False),
    ("journal.reverse", True, False, True, False, False),
    ("journal_template.manage", True, True, True, False, False),
    ("budget.view", True, True, True, True, False),
    ("budget.edit", True, True, True, False, False),
    ("asset.view", True, True, True, True, False),
    ("asset.edit", True, True, True, False, False),
    ("asset.depreciate", True, False, True, False, False),
    ("depreciation_model.manage", True, True, True, False, False),
    ("tax_code.manage", True, False, False, False, False),
    ("intercompany.post", True, False, True, False, False),
    ("intercompany.reverse", True, False, True, False, False),
    ("period.close", True, False, True, False, False),
    ("branch.manage", True, True, True, False, False),
    ("project.view", True, True, True, True, False),
    ("project.edit", True, True, True, False, False),
    ("project.delete", True, False, True, False, False),
    ("time_entry.create", True, True, True, False, True),
    ("time_entry.approve", True, False, True, False, True),
    ("employee.view", True, True, True, True, True),
    ("employee.edit", True, True, True, False, True),
    ("employee.tfn_view", True, False, False, False, True),
    ("pay_run.create", True, False, True, False, True),
    ("pay_run.post", True, False, True, False, True),
    ("leave.manage", True, False, True, False, True),
    ("super_fund.view", True, True, True, True, True),
    ("super_fund.edit", True, False, True, False, True),
    ("super_lodgement.create", True, False, True, False, True),
    ("super_lodgement.finalise", True, False, True, False, True),
    ("payroll.run", True, False, True, False, True),
    ("tpar.create", True, False, True, False, True),
    ("tpar.finalise", True, False, True, False, True),
    ("report.view", True, True, True, True, False),
    ("report.export", True, True, True, False, False),
    ("bas.prepare", True, True, True, False, False),
    ("bas.lodge", True, False, True, False, False),
    ("tax_return.create", True, False, True, False, False),
    ("tax_return.lodge", True, False, True, False, False),
    ("ato_sbr.keystore.manage", True, False, False, False, False),
    ("ato_sbr.onboarding", True, False, False, False, False),
    ("user.admin", True, False, False, False, False),
    ("permission.manage", True, False, False, False, False),
    ("company.delete", True, False, False, False, False),
    ("settings.edit", True, False, False, False, False),
    ("company.export", True, False, False, False, False),
    ("integration.configure", True, False, False, False, False),
    ("audit.view", True, True, True, False, False),
    ("audit.export", True, False, False, False, False),
    ("import.run", True, False, True, False, False),
    ("api_token.manage", True, False, False, False, False),
    ("principal_grant.manage", True, False, False, False, False),
    ("billing.manage", True, False, False, False, False),
)

# roles.name -> column index into each GRANTS tuple (1=owner_admin,
# 2=bookkeeper, 3=approver, 4=readonly, 5=payroll).
ROLE_NAME_TO_COLUMN: dict[str, int] = {
    "Owner": 1,
    "Admin": 1,
    "Bookkeeper": 2,
    "Approver": 3,
    "Read-only": 4,
    "Payroll-only": 5,
}


def codes_for_role_name(name: str) -> frozenset[str]:
    """Return the starter grant set for one of the six starter role names.

    Empty frozenset for any name not in ``ROLE_NAME_TO_COLUMN`` (a
    genuinely custom role — those start with zero grants, an admin
    must explicitly build them up via ``PATCH /api/v1/roles/{id}``).
    """
    column = ROLE_NAME_TO_COLUMN.get(name)
    if column is None:
        return frozenset()
    return frozenset(row[0] for row in GRANTS if row[column])


__all__ = ["GRANTS", "ROLE_NAME_TO_COLUMN", "codes_for_role_name"]
