"""role_permissions — wire to the roles table + FORCE RLS + D1-corrected
starter-role grid (granular_permissions, D1/D2/D5).

Background
----------
Before this migration, ``role_permissions`` was a GLOBAL table keyed
on a bare 5-value role STRING (``owner, admin, accountant, bookkeeper,
viewer``), shared identically by every tenant — the exact latent bug
``permission-matrix-draft.md``'s "Schema gaps" §2 describes: the
moment role customisation is wired, the first tenant to edit
"bookkeeper" changes it for every tenant on the instance. It also
carries the WRONG grants for a segregation-of-duties reading —
Richard's D1: "the live seed wrongly lets bookkeeper post everything"
(``invoice.post``, ``bill.post``, ``payment.post``, ``credit_note.
post``, ``journal.post``, ``asset.depreciate`` were all granted to
bookkeeper by migration 0033).

What this migration does
-------------------------
1. Adds ``role_id`` (FK -> ``roles.id``) and ``tenant_id`` (FK ->
   ``tenants.id``, denormalised for the RLS predicate) to
   ``role_permissions``, both nullable initially.
2. **Deletes every existing row.** The old rows are GLOBAL (one set
   shared by all tenants) and cannot be mapped 1:1 onto the new
   per-tenant-per-role shape — there is no correct ``role_id`` to
   backfill an old ``role='bookkeeper'`` row onto, because there are
   now N tenants × 1 Bookkeeper role each, not one. Nothing in this
   codebase enforces anything via ``role_permissions`` today
   (``require_permission()`` is called from zero routers — confirmed
   by the draft's own audit) so this delete has ZERO live behavioural
   effect until enforcement is wired in a later commit on this same
   branch; the finalized-matrix reseed below is a straight replacement,
   not a data-loss risk to any running install.
3. Reseeds fresh: for every tenant, for each of its six starter roles
   (seeded by 0190 / self-healed by ``services.roles.
   ensure_starter_roles``), inserts the finalized-matrix grant set —
   ``_GRANTS`` below, transcribed row-for-row from the approved draft
   with D1 applied (bookkeeper never gets a post/void/lodge-class
   code) and D5 applied (Owner and Admin get IDENTICAL grants,
   including ``billing.manage`` — Richard explicitly rejected the
   draft's "Owner-only" proposal for that code: "leave Owner/Admin as
   permission-twins for now").
4. Sets ``role_id``/``tenant_id`` NOT NULL, drops the old composite PK
   (``role``, ``permission_code``) + the old ``ck_role_permissions_role``
   CHECK + the old ``role`` string column, adds a new composite PK on
   (``role_id``, ``permission_code``).
5. ENABLE + FORCE ROW LEVEL SECURITY + the standard symmetric
   ``tenant_isolation`` policy (deferred until after the reseed, same
   "data first, FORCE second" precedent as 0058/0186/0190).

Tenant-scoping checklist (see new-table-rls-checklist):
[x] tenant_id NOT NULL column
[x] FK to tenants(id)
[x] ENABLE ROW LEVEL SECURITY + FORCE ROW LEVEL SECURITY
[x] CREATE POLICY tenant_isolation (USING + WITH CHECK)
[x] Index on (tenant_id, ...)
[x] Service-layer filter as defence-in-depth (services/permissions.py)
[x] Always-set on writes (services/roles.py stamps tenant_id from the
    owning role; services/permissions.py never defaults it)
[x] Cross-tenant probe test added (tests/test_rls_role_permissions.py)
[x] RLS probe test added (same file)

Reversibility
-------------
``downgrade()`` restores the original 5-value CHECK-constrained
``role`` string column shape and reseeds it verbatim from the original
0033/0111 grant lists (the historical, D1-UNCORRECTED grants — this
is a faithful schema-shape reversal, not a data-preservation guarantee
for anything written after this migration ran forward, matching the
0058 downgrade precedent). Drops ``roles``-table dependence entirely.

Revision ID: 0194_role_permissions_rls
Revises:     0193_users_role_id
Create Date: 2026-07-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0194_role_permissions_rls"
down_revision: str | None = "0193_users_role_id"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "role_permissions"

_USING = "(tenant_id = current_setting('app.current_tenant', true)::uuid)"
_WITH_CHECK = _USING

_BASE_ROLE_VALUES = "'owner', 'admin', 'accountant', 'bookkeeper', 'viewer'"

# ---------------------------------------------------------------------------
# The finalized-matrix grid — transcribed row-for-row from the approved
# permission-matrix-draft.md, D1 applied (bookkeeper column is never
# True for a post/void/lodge-class code) and D5 applied (Owner == Admin
# always, including billing.manage).
#
# (code, owner_admin, bookkeeper, approver, readonly, payroll)
# ---------------------------------------------------------------------------
_GRANTS: tuple[tuple[str, bool, bool, bool, bool, bool], ...] = (
    ("dashboard.view", True, True, True, True, True),
    # --- 1. Sales / Accounts Receivable ------------------------------ #
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
    # --- 2. Purchases / Accounts Payable ------------------------------ #
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
    # --- 3. Payments (spans AR and AP) -------------------------------- #
    ("payment.view", True, True, True, True, False),
    ("payment.create", True, True, True, False, False),
    ("payment.post", True, False, True, False, False),
    ("payment.delete", True, True, True, False, False),
    ("payment.void", True, False, True, False, False),
    # --- 4. Banking ----------------------------------------------------- #
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
    # --- 5. Accounting / General Ledger ---------------------------------- #
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
    # --- 6. Projects & Time Tracking ---------------------------------- #
    ("project.view", True, True, True, True, False),
    ("project.edit", True, True, True, False, False),
    ("project.delete", True, False, True, False, False),
    ("time_entry.create", True, True, True, False, True),
    ("time_entry.approve", True, False, True, False, True),
    # --- 7. Payroll ------------------------------------------------- #
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
    # --- 8. Reports --------------------------------------------------- #
    ("report.view", True, True, True, True, False),
    ("report.export", True, True, True, False, False),
    # --- 9. Compliance / Lodgement -------------------------------------- #
    ("bas.prepare", True, True, True, False, False),
    ("bas.lodge", True, False, True, False, False),
    ("tax_return.create", True, False, True, False, False),
    ("tax_return.lodge", True, False, True, False, False),
    ("ato_sbr.keystore.manage", True, False, False, False, False),
    ("ato_sbr.onboarding", True, False, False, False, False),
    # --- 10. Admin / System --------------------------------------------- #
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
    # D5: Richard rejected the draft's owner-exclusive differentiation
    # for billing.manage ("leave Owner/Admin as permission-twins for
    # now") — granted to both, like every other admin-tier code.
    ("billing.manage", True, False, False, False, False),
)

# roles.name -> column index in each _GRANTS tuple (1=owner_admin,
# 2=bookkeeper, 3=approver, 4=readonly, 5=payroll). "Owner" and "Admin"
# both read the owner_admin column (D5 — identical grants).
_ROLE_NAME_TO_COLUMN: dict[str, int] = {
    "Owner": 1,
    "Admin": 1,
    "Bookkeeper": 2,
    "Approver": 3,
    "Read-only": 4,
    "Payroll-only": 5,
}


def upgrade() -> None:
    conn = op.get_bind()

    # ---- Step 1: add role_id / tenant_id NULLABLE. ---------------------
    op.add_column(
        _TABLE,
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # ---- Step 2: delete the old global rows — see module docstring for
    # why these cannot be backfilled 1:1 onto the new per-tenant shape,
    # and why this has zero live behavioural effect (require_permission
    # is called from zero routers as of this migration). --------------
    conn.execute(sa.text(f"DELETE FROM {_TABLE}"))

    # ---- Step 2a: relax the LEGACY ``role`` column to nullable before
    # the reseed. It is still present (still NOT NULL, from migration
    # 0033) at this point — it isn't dropped until Step 4 below — and
    # the Step 3 INSERTs never populate it (the new per-tenant shape
    # has no use for it), so without this the very first insert trips
    # the old NOT NULL constraint. Caught by the docker test suite
    # running a genuine `alembic upgrade head` against a fresh database
    # (host-side ruff/import/alembic-heads checks don't execute a
    # migration's DML, so this shipped in the first 14 commits on this
    # branch undetected until the suite ran). Dropping NOT NULL here is
    # safe: the column is dropped outright two steps later, and a NULL
    # value never violates the still-live ``ck_role_permissions_role``
    # CHECK (a CHECK constraint passes when its expression evaluates to
    # UNKNOWN, i.e. any operand is NULL, unless it explicitly tests
    # IS NOT NULL — this one does not).
    #
    # ``role`` is also still part of the OLD composite primary key
    # (``role``, ``permission_code``) at this point, and Postgres
    # refuses ``ALTER COLUMN ... DROP NOT NULL`` on a PK member
    # ("column is in a primary key") — the second bug this same
    # `alembic upgrade head` run against a fresh database surfaced, on
    # the retry after the first fix above. Drop the old PK constraint
    # here (pulled forward from Step 4, which no longer needs to drop
    # it) before relaxing nullability.
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT pk_role_permissions"))
    op.alter_column(_TABLE, "role", nullable=True)

    # ---- Step 3: reseed per tenant, per starter role, from _GRANTS. ---
    role_rows = conn.execute(
        sa.text("SELECT id, tenant_id, name FROM roles WHERE is_system = true")
    ).all()
    for role_id, tenant_id, name in role_rows:
        column = _ROLE_NAME_TO_COLUMN.get(name)
        if column is None:
            continue
        codes = [row[0] for row in _GRANTS if row[column]]
        for code in codes:
            conn.execute(
                sa.text(
                    f"""
                    INSERT INTO {_TABLE} (role_id, tenant_id, permission_code)
                    VALUES (:role_id, :tenant_id, :code)
                    """
                ).bindparams(role_id=role_id, tenant_id=tenant_id, code=code)
            )

    # ---- Step 4: drop the legacy shape, constrain the new columns. ----
    # (old PK already dropped in Step 2a, ahead of the reseed.)
    op.execute(
        sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT ck_role_permissions_role")
    )
    op.drop_index("ix_role_permissions_role", table_name=_TABLE)
    op.drop_column(_TABLE, "role")

    op.alter_column(_TABLE, "role_id", nullable=False)
    op.alter_column(_TABLE, "tenant_id", nullable=False)
    op.create_foreign_key(
        f"{_TABLE}_role_id_fkey", _TABLE, "roles", ["role_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        f"{_TABLE}_tenant_id_fkey", _TABLE, "tenants", ["tenant_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_primary_key(
        "pk_role_permissions", _TABLE, ["role_id", "permission_code"]
    )
    op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])
    op.create_index(f"ix_{_TABLE}_role_id", _TABLE, ["role_id"])

    # ---- Step 5: ENABLE + FORCE RLS + standard symmetric policy. ------
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {_TABLE} "
            f"FOR ALL USING {_USING} WITH CHECK {_WITH_CHECK}"
        )
    )
    op.execute(sa.text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))


# ---------------------------------------------------------------------------
# Downgrade — restores the original 0033/0111 global shape + grants.
# ---------------------------------------------------------------------------

_LEGACY_ROLE_GRANTS: dict[str, list[str]] = {
    # Verbatim from 0033_permissions._ROLE_GRANTS + 0111's additions —
    # the historical, D1-UNCORRECTED grants (schema-shape reversal, not
    # a claim that this matches whatever was live at the moment upgrade()
    # ran — see module docstring).
    "admin": [
        "dashboard.view", "contact.view", "account.view", "invoice.view",
        "bill.view", "credit_note.view", "payment.view", "journal.view",
        "report.view", "asset.view", "bank.view", "item.view",
        "project.view", "budget.view", "contact.edit", "invoice.create",
        "bill.create", "payment.create", "credit_note.create",
        "journal.draft", "item.edit", "project.edit", "invoice.post",
        "bill.post", "payment.post", "credit_note.post", "journal.post",
        "journal.reverse", "asset.edit", "asset.depreciate", "budget.edit",
        "account.edit", "bank.sync", "invoice.void", "bill.void",
        "payment.void", "period.close", "bas.lodge", "payroll.run",
        "user.admin", "company.delete", "integration.configure",
        "audit.export", "company.export", "settings.edit",
        "employee.view", "employee.edit", "employee.tfn_view",
        "super_fund.view", "super_fund.edit",
    ],
    "accountant": [
        "dashboard.view", "contact.view", "account.view", "invoice.view",
        "bill.view", "credit_note.view", "payment.view", "journal.view",
        "report.view", "asset.view", "bank.view", "item.view",
        "project.view", "budget.view", "contact.edit", "invoice.create",
        "bill.create", "payment.create", "credit_note.create",
        "journal.draft", "item.edit", "project.edit", "invoice.post",
        "bill.post", "payment.post", "credit_note.post", "journal.post",
        "journal.reverse", "asset.edit", "asset.depreciate", "budget.edit",
        "account.edit", "bank.sync", "invoice.void", "bill.void",
        "payment.void", "period.close", "bas.lodge", "payroll.run",
        "employee.view", "employee.edit", "employee.tfn_view",
        "super_fund.view", "super_fund.edit",
    ],
    "bookkeeper": [
        "dashboard.view", "contact.view", "contact.edit", "account.view",
        "invoice.view", "invoice.create", "invoice.post", "bill.view",
        "bill.create", "bill.post", "credit_note.view",
        "credit_note.create", "payment.view", "payment.create",
        "payment.post", "journal.view", "journal.draft", "report.view",
        "asset.view", "bank.view", "bank.sync", "item.view", "item.edit",
        "project.view", "budget.view", "employee.view", "employee.edit",
        "super_fund.view",
    ],
    "viewer": [
        "dashboard.view", "contact.view", "account.view", "invoice.view",
        "bill.view", "credit_note.view", "payment.view", "journal.view",
        "report.view", "asset.view", "bank.view", "item.view",
        "project.view", "budget.view", "employee.view", "super_fund.view",
    ],
    "owner": [  # 0058: owner = copy of admin's grants
        "dashboard.view", "contact.view", "account.view", "invoice.view",
        "bill.view", "credit_note.view", "payment.view", "journal.view",
        "report.view", "asset.view", "bank.view", "item.view",
        "project.view", "budget.view", "contact.edit", "invoice.create",
        "bill.create", "payment.create", "credit_note.create",
        "journal.draft", "item.edit", "project.edit", "invoice.post",
        "bill.post", "payment.post", "credit_note.post", "journal.post",
        "journal.reverse", "asset.edit", "asset.depreciate", "budget.edit",
        "account.edit", "bank.sync", "invoice.void", "bill.void",
        "payment.void", "period.close", "bas.lodge", "payroll.run",
        "user.admin", "company.delete", "integration.configure",
        "audit.export", "company.export", "settings.edit",
        "employee.view", "employee.edit", "employee.tfn_view",
        "super_fund.view", "super_fund.edit",
    ],
}


def downgrade() -> None:
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY"))
    op.drop_index(f"ix_{_TABLE}_role_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_tenant_id", table_name=_TABLE)
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT pk_role_permissions"))
    op.drop_constraint(f"{_TABLE}_tenant_id_fkey", _TABLE, type_="foreignkey")
    op.drop_constraint(f"{_TABLE}_role_id_fkey", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "tenant_id")
    op.drop_column(_TABLE, "role_id")

    op.add_column(_TABLE, sa.Column("role", sa.String(16), nullable=True))
    conn = op.get_bind()
    for role, codes in _LEGACY_ROLE_GRANTS.items():
        for code in codes:
            conn.execute(
                sa.text(
                    f"INSERT INTO {_TABLE} (role, permission_code) "
                    f"VALUES (:role, :code)"
                ).bindparams(role=role, code=code)
            )
    op.alter_column(_TABLE, "role", nullable=False)
    op.create_index("ix_role_permissions_role", _TABLE, ["role"])
    op.execute(
        sa.text(
            f"ALTER TABLE {_TABLE} ADD CONSTRAINT ck_role_permissions_role "
            f"CHECK (role IN ({_BASE_ROLE_VALUES}))"
        )
    )
    op.create_primary_key(
        "pk_role_permissions", _TABLE, ["role", "permission_code"]
    )
