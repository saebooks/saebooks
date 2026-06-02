# Audit-log coverage — design pitch

**Status:** Draft for Richard's review
**Drafted:** 2026-05-27
**Closes:** P0-C from round-2 critic audit (Critics 02, 07, 13, 18)
**Related:** [[feedback_new-table-rls-checklist]], [[saebooks-strategy-api-first]]

---

## Problem

Production `audit_log` has **1 row across the whole DB**. The only writer is `services/hard_delete.py::log_hard_delete`. Every other compliance-relevant state transition — invoice POST, bill POST, JE post with period-lock override, payment POST, contact archive, account is_header toggle, period lock set/unset, BAS lodgement finalise, TPAR finalise, pay-run post, user role change, API token issue/revoke — leaves no audit trail.

This is **ATO-indefensible** for a BAS-facing system. The first audit a real customer faces, we cannot answer "who changed this and when". The `change_log` table captures *what diff* but its `actor` field is the JWT prefix (`api:eyJhbGci…`), not a user UUID — un-attributable.

## What's already right

The `audit_log` table schema is fine:

```
audit_log:
  id            uuid PK
  tenant_id     uuid NOT NULL   -- FORCE RLS + tenant_isolation policy
  actor_user_id uuid NOT NULL   -- already correct type (not JWT prefix)
  action        text NOT NULL
  table_name    text NOT NULL
  row_id        text NOT NULL
  row_snapshot  jsonb NOT NULL  -- full row at the moment of action
  reason        text             -- optional override reason etc.
  at            timestamptz NOT NULL DEFAULT now()
```

Indexes (`ix_audit_log_table_row`, `ix_audit_log_tenant_at`) are right for the two query patterns: "show me everything that happened to row X" and "show me everything tenant Y did in date range Z". RLS is forced + tenant-scoped. No schema change needed.

## What's missing

Coverage. The 14 events below need an `audit_log` row, written **inside the same transaction** as the action (so a crash mid-action can't drop the audit row).

| # | Event | Service function today | action enum value |
|---|-------|------------------------|---------------------|
| 1 | Invoice DRAFT→POSTED | `services/invoices.py::api_post` | `invoice.post` |
| 2 | Invoice POSTED→VOIDED | `services/invoices.py::api_void_invoice` | `invoice.void` |
| 3 | Invoice hard_delete | `services/hard_delete.py` | `invoice.hard_delete` (covered) |
| 4 | Bill DRAFT→POSTED | `services/bills.py::api_post` | `bill.post` |
| 5 | Bill POSTED→VOIDED | `services/bills.py::api_void_bill` | `bill.void` |
| 6 | Payment DRAFT→POSTED | `services/payments.py::post_payment` | `payment.post` |
| 7 | Payment POSTED→VOIDED | `services/payments.py::api_void_payment` | `payment.void` |
| 8 | Credit-note DRAFT→POSTED | `services/credit_notes.py::post_credit_note` | `credit_note.post` |
| 9 | JE post with period-lock override | `services/journal.py::post` (override branch) | `journal.override_post` |
| 10 | Contact archive | `services/contacts.py::archive` | `contact.archive` |
| 11 | Period lock set / unset | wherever period_locks is mutated | `period_lock.set` / `period_lock.clear` |
| 12 | BAS lodgement finalise | `services/tax_returns.py::finalise` (G1 work) | `bas.finalise` |
| 13 | TPAR finalise | `services/tpar.py::finalise` (G8 work) | `tpar.finalise` |
| 14 | Pay-run post | `services/pay_runs.py::post` (stub today) | `pay_run.post` |
| 15 | API token issue / revoke | `services/api_tokens.py` | `api_token.issue` / `api_token.revoke` |

Account-level (rare, but compliance-relevant): `account.create`, `account.archive`, `account.is_header_toggle`. User-level: `user.role_change`. Both can be deferred to v2 of this work — they're not in the BAS hot path.

## Action enum

Today `audit_log.action` is plain `text`. Two paths:

- **(A)** Promote to a Postgres enum `audit_action_enum` with the values above. Pros: prevents typos, enables enum-keyed analytics. Cons: every new action needs a migration.
- **(B)** Keep as text, enforce via a Python `class AuditAction(StrEnum)` in `services/audit_log.py`. Pros: cheap to extend. Cons: typos at the call site silently produce drift.

**Recommendation: B.** The text column + Python enum mirrors how `expense_status_enum` etc. are enforced at the app layer with check constraints at the DB. New actions land without a migration. Schema-level enum can be added later as a hardening step once the list stabilises.

## Helper function

Add `services/audit_log.py`:

```python
async def append(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    action: str,                  # AuditAction.<value>
    table_name: str,
    row_id: str,                  # str(uuid) or text id
    row_snapshot: dict,           # serialised row at point of action
    reason: str | None = None,
) -> None:
    session.add(AuditLog(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        table_name=table_name,
        row_id=row_id,
        row_snapshot=row_snapshot,
        reason=reason,
    ))
    # No flush here — the caller's commit flushes the action + audit together.
```

Critically: **no flush, no commit** — the caller controls the transaction. If the action commits, the audit row commits with it. If the action rolls back (e.g. period-lock PostingError), the audit row rolls back too. No orphan audits.

## Actor resolution

The auth dependency `get_active_user_id` should be added (alongside `get_active_tenant_id`, `get_active_company_id`) and threaded into every service function that writes an audit. It resolves the user UUID from the JWT subject claim or API token's bound user via a `users` join — never returns the JWT prefix.

This is a small lift in `saebooks/api/v1/deps.py`. Once the dep is wired, every action's signature gains `actor_user_id: uuid.UUID` (replacing the current `actor: str` which is freeform).

## row_snapshot policy

Snapshot the **full Pydantic Out shape** at the moment of action (not the raw DB row, not a diff). Rationale:
- Diff can be reconstructed from snapshot N and snapshot N-1
- Out shape is the API contract — a snapshot is what the customer would have seen if they'd GET'd that row at that moment
- jsonb storage means we can query into specific fields later if needed (`row_snapshot->>'amount'`)

Snapshot before any post-action mutation (e.g. status flip), so `row_snapshot` shows the row *as it became after the action*. To capture the pre-action state, take `from_snapshot` from an earlier audit row or from `change_log`.

## reason field policy

`reason` is for **action-specific context** the action itself carries:
- Period-lock override: the override reason text Richard typed
- Void: the void reason
- Archive: the archive reason
- Hard delete: the hard-delete reason (already populated)

Leave `reason` NULL for actions where there's no operator-provided rationale (a routine POST doesn't need one).

## Backfill policy

**Don't backfill.** Historic transitions were unauditable; pretending otherwise by synthesising rows would be worse than the gap. Document in `saebooks/docs/audit-log.md`: "audit_log coverage begins at <go-live SHA>; transitions before that date are unauditable by design."

## Rollout order

1. Land `services/audit_log.py` + `AuditAction` enum + `get_active_user_id` dep — one PR, zero behaviour change.
2. Wire the 6 hot-path POST/VOID actions (invoice, bill, payment, credit-note, JE-override, pay-run) — second PR, all guarded by tests asserting the audit row appears in the same transaction.
3. Wire the finalisation actions (BAS, TPAR) — third PR.
4. Wire the rare ones (period_lock, contact.archive, api_token issue/revoke) — fourth PR.
5. Add `GET /api/v1/audit-log?row_id=...&table=...` read endpoint with admin-only access — last PR, makes the trail visible.

Each PR independently mergeable. PR 1 lands without any user-visible change; PRs 2-4 begin producing audit rows; PR 5 surfaces them.

## Estimated effort

- PR 1 (foundations): 2-3 hours
- PR 2 (hot-path 6 actions): 3-4 hours
- PR 3 (finalisations): 1-2 hours
- PR 4 (rare): 1-2 hours
- PR 5 (read endpoint + admin UI): 2-3 hours
- Tests: 1.5x each (so add ~10 hours)

**Total: ~20-25 hours of focused work.** First useful audit rows start appearing after PR 2.

## Open questions for Richard

1. **AuditAction text vs enum** — keep as text + Python enum, or promote to Postgres enum? (Recommendation: text.)
2. **API token actor** — when an action is initiated by an API token (not a user session), should `actor_user_id` be the user the token is bound to, or do we need a separate `actor_token_id` column? (Recommendation: token-bound user; if the token is compromised, the audit trail correctly points to the user whose token was used.)
3. **Pay-run audit on each row vs the run itself** — log one `pay_run.post` action per pay-run, or one action per `pay_run_line`? (Recommendation: per pay-run, with `row_snapshot` containing the line summaries.)
4. **Whether to write `audit.read` rows** for sensitive reads — i.e. someone GET'ing the audit log itself? (Recommendation: yes, for /audit-log endpoint specifically; otherwise no, to avoid log-amplification.)
