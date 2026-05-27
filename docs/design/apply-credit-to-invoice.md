# Apply credit to invoice — design pitch

**Status:** Draft for Richard's review
**Drafted:** 2026-05-27
**Closes:** P0-H from round-2 critic audit (Critic 13)
**Related:** [[saebooks-strategy-api-first]], audit-log-coverage pitch

---

## Problem

A customer has an open invoice and an unallocated credit note. Today there is **no way to apply the credit to the invoice** — no UI button, no API endpoint, no service function, no schema. Critic 13's wording: "to apply a $200 CN against INV-001 you'd need either a new entity or to relax the XOR on payment_allocations."

This is a daily bookkeeper workflow. Customer X pays an invoice late and we issue them a $50 goodwill credit; the next invoice should net the credit. Today that's only possible via a manual JE, which bypasses the sub-ledger so the customer's AR Aging report still shows two open items.

## What "apply credit" should do

Customer X state:
- INV-100: total $1100, amount_paid $0, outstanding $1100
- CN-50: total $550, amount_allocated $0, outstanding $550

After `apply_credit(credit_note=CN-50, invoice=INV-100, amount=$550)`:
- INV-100: amount_paid $550, outstanding $550
- CN-50: amount_allocated $550, outstanding $0
- AR control account: **unchanged** (the CN post already moved it $550 against AR)
- Customer aging: one open invoice for $550, zero open credits

**Critically: no new GL journal entry is needed.** The credit-note's own posting already moved $550 from AR to Income + GST control. The application step is sub-ledger allocation only — it tells the system "this $550 of AR that CN-50 cleared was specifically the INV-100 $550 portion".

## Option A — `credit_applications` table (recommended)

```sql
CREATE TABLE credit_applications (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  company_id        uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  credit_note_id    uuid NOT NULL REFERENCES credit_notes(id) ON DELETE RESTRICT,
  invoice_id        uuid NOT NULL REFERENCES invoices(id)     ON DELETE RESTRICT,
  amount            numeric(18,2) NOT NULL CHECK (amount > 0),
  applied_date      date NOT NULL,
  applied_by        varchar(64),
  notes             text,
  reverse_je_id     uuid REFERENCES journal_entries(id),     -- NULL unless application is reversed
  reversed_at       timestamptz,
  version           int NOT NULL DEFAULT 1,
  created_at        timestamptz NOT NULL DEFAULT now(),
  archived_at       timestamptz
);

-- FORCE RLS + tenant_isolation policy [per [[feedback_new-table-rls-checklist]]]
ALTER TABLE credit_applications ENABLE ROW LEVEL SECURITY;
ALTER TABLE credit_applications FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON credit_applications
  USING (tenant_id = current_setting('app.current_tenant', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::uuid);

-- Two-sided allocation cap — enforced in service, mirrored by trigger
-- so direct SQL can't oversubscribe a credit note or pay an invoice past
-- its total. Equivalent to the check_allocation_cap on payment_allocations.
```

### Service contract

```python
async def apply_credit_to_invoice(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    tenant_id: uuid.UUID,
    credit_note_id: uuid.UUID,
    invoice_id: uuid.UUID,
    amount: Decimal,
    applied_date: date,
    actor: str,
) -> CreditApplication:
    """Apply a posted credit note against an open invoice.

    Validates:
    - both belong to same company + tenant
    - both reference the same customer contact (or both are one_off_customer, same id)
    - credit_note is POSTED and not archived
    - invoice is POSTED and not voided
    - amount > 0 and amount <= min(invoice.outstanding, credit_note.outstanding)
    - applied_date not inside locked period (uses same period_lock check
      as JE post — even though no JE is created, the allocation date
      must respect the lock for BAS-period correctness)

    Inserts credit_applications row, bumps invoice.amount_paid and
    credit_note.amount_allocated, writes an audit_log row, returns the
    application.

    No JE is created — the credit_note's own posting already moved AR.
    """
```

### API surface

```
POST /api/v1/credit-notes/{credit_note_id}/apply
Body: { "invoice_id": uuid, "amount": decimal, "applied_date": date, "notes": str | null }
Response: CreditApplicationOut (with version, applied_by, etc.)

GET /api/v1/credit-notes/{id}/applications      — list applications of this CN
GET /api/v1/invoices/{id}/credit-applications   — list CNs applied to this invoice
POST /api/v1/credit-applications/{id}/reverse   — reverse an application
```

### Reversal path

A credit application can be undone. The reversal:
1. INSERT a reversing `credit_applications` row with `amount = -original` and `reverse_je_id` (optional — only if we want a corresponding JE marker; recommended NO since the original application had no JE)
2. UPDATE invoice.amount_paid -= original
3. UPDATE credit_note.amount_allocated -= original
4. UPDATE the original application's `reversed_at = now()`
5. audit_log row

### UI

- On `/invoices/{id}` — section "Available credits" listing open credit-notes for this contact, "Apply" button → modal asking for amount + applied_date
- On `/credit-notes/{id}` — section "Apply to invoice" listing open invoices for this contact
- On both — "History" list of applications

### Pros

- Clean shape: credit application is its own concept.
- No XOR weirdness, no $0 payments.
- Easy to reverse independently of the underlying CN or invoice.
- credit_applications becomes the source-of-truth sub-ledger; invoice.amount_paid and credit_note.amount_allocated are materialised views of it.

### Cons

- New table → new migration, new RLS policy, new model, new schema, new service, new route, new templates. ~7-9 files.
- Customer-aging report needs to learn about credit_applications when computing outstanding (today it reads `invoice.amount_paid` which we'll keep maintained, so the report is unaffected).

## Option B — Relax `payment_allocations` XOR

Drop the `ck_xor_target` constraint and let a single payment_allocation row carry BOTH `invoice_id` AND `credit_note_id`. The "apply" action creates a fake Payment with `amount = 0` and an allocation pairing the two.

### Pros

- Reuses existing `payment_allocations` table.
- No new migration for a table.

### Cons

- $0 payments aren't really payments — they pollute the payments list.
- payment.amount = sum(allocations.amount) invariant breaks (or you have to special-case zero-sum applications).
- The aggregator helpers `_refresh_invoice_amount_paid` / `_refresh_bill_amount_paid` currently filter on `payment_allocations.invoice_id`; they'd need a new branch.
- Semantics overloaded: a "payment allocation" should mean "this much cash went to this document". Pairing two non-cash documents in one allocation row is conceptually wrong.

### Verdict

Not recommended. The constraint was deliberate; relaxing it is structurally worse.

## Trade-off summary

| Question | Option A (table) | Option B (relax XOR) |
|----------|------------------|----------------------|
| Schema cost | New table | Drop constraint |
| Conceptual clarity | High | Low |
| Re-uses existing aggregators | No (adds parallel one) | Partially (with hack) |
| Reversal path | Clean | Awkward |
| BAS / aged-AR impact | None | None |
| Effort | ~12-15 hours | ~6-8 hours |

**Pick Option A.** The 4-7 hour delta buys structural correctness that doesn't have to be paid back later.

## Open questions for Richard

1. **Direction**: should we also support applying a **vendor credit** to a bill (supplier-side mirror)? The same shape would apply with `vendor_credit_applications` (note `credit_notes` today is customer-side only — supplier credits would need their own table or repurposing). Suggest scoping to customer side for v1.

2. **Multi-currency**: if the CN is in USD and the invoice in AUD, do we convert at the application date's FX rate? Or refuse cross-currency applications? (Recommendation: refuse for v1; force the user to convert at issuance.)

3. **Partial credit application across multiple invoices**: should one `apply_credit_to_invoice` call support multiple invoices, or one-at-a-time with separate calls? (Recommendation: one-at-a-time. Bulk UI can issue N calls sequentially. Keeps the audit/reversal trail per-application.)

4. **Customer-statement rendering**: when a CN is fully applied, should it still appear on the statement? (Recommendation: yes, with "Applied to INV-100" annotation. Hide only on archive.)

5. **Eligibility — what stops you applying a credit cross-customer?** Strict check: the CN's `contact_id` must equal the invoice's `contact_id`. For `one_off_customer_id` CNs, refuse (or: promote the one-off to a contact first). Defer the latter to the apply-credit-to-one_off flow.
