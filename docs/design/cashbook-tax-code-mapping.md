# Cashbook tax_code mapping — design pitch

**Status:** Draft for Richard's review
**Drafted:** 2026-05-27
**Closes:** P0-B cashbook-portion from round-2 critic audit (Critic 06)
**Related:** audit-log-coverage pitch, apply-credit pitch

---

## Problem

`record_cashbook_entry` builds journal_lines with `account_id`, `debit`, `credit`, `description`, and `gst_amount` — but **no `tax_code_id`**. The BAS aggregator's query at `services/tax_engine/au.py:340-344` filters journal_lines by their tax_code's `reporting_type`, so a NULL tax_code_id makes the cashbook line invisible to G1 / G11 / G10 / G3 etc.

Net effect: a sole-trader using cashbook mode posts a $1,100 GST-inclusive sale, sees the JE in the books, and then their BAS Summary shows G1 = $0 for that period. The income exists on the P&L but is invisible to the BAS. Same issue on the expense side for G11 (non-capital purchases).

## Why it's not as small as it looks

The obvious fix — "look up the company's GST tax_code and stamp it on the cashbook line" — fails because:

1. **There are two 10%-rate tax codes in a normal AU CoA**:
   - `33 GST` (taxable supplies — what you charge customers)
   - `41 GST on purchases` (taxable acquisitions — what you claim back)

   Rate alone doesn't disambiguate which to use. Direction (income vs expense) does, but not robustly — `EXP_BANK` and `EXP_SUPER` are expense-direction with 0% GST and should use `32 GST free`, not `41`.

2. **Capital purchases need a different reporting_type**. BAS G10 is "non-capital purchases (capital)" — i.e. purchases that go to a capital asset account and report under G10 not G11. The current primary tenant has only `reporting_type IN ('taxable', 'gst_free')`. No `capital` code. `CAP_PURCHASE` cashbook category would land in G11 wrongly.

3. **Code names aren't standardised across tenants.** Sauer happens to have `33` and `41`; another tenant's seed might use `GST` and `GSTP`, or `GST_COLLECTED` and `GST_PAID`. The seed is `saebooks/seed/au/tax_codes.csv` but tenants can amend.

## Recommended design

Two-part change. Both required.

### Part 1 — Add `tax_code` field to `CashbookCategory`

```python
@dataclass(frozen=True)
class CashbookCategory:
    code: str
    direction: str
    default_account_code: str | None
    gst_default: Decimal
    tax_code: str | None       # NEW: tax_codes.code to apply on the JE line
    reporting_type: str        # NEW: derived hint — "taxable", "gst_free", "capital"
    ...
```

`tax_code` is the **string code** (e.g. `"33"`, `"41"`, `"32"`), not a UUID — categories ship in source and don't know about per-company UUIDs. The resolver looks up the actual UUID at JE-build time from `tax_codes` filtered by `(company_id, code=cat.tax_code, archived_at IS NULL)`.

`reporting_type` is a redundancy used by the resolver fallback: if the company doesn't have the named code, fall back to any active tax_code matching the reporting_type. This way `EXP_BANK` still produces a GST-free line even if the tenant renamed `32 GST free` to `FRE` or similar.

### Part 2 — Per-category mapping table

Standard AU mapping for the 20 categories:

| Category | direction | gst_default | tax_code | reporting_type | notes |
|----------|-----------|-------------|----------|----------------|-------|
| INC_SALES        | income   | 0.10 | `33` | taxable  | G1 |
| INC_SERVICES     | income   | 0.10 | `33` | taxable  | G1 |
| INC_INTEREST     | income   | 0.00 | `32` | input_taxed | input-taxed in AU; tenants without it fall back to gst_free |
| INC_OTHER        | income   | 0.10 | `33` | taxable  | G1 |
| EXP_VEHICLE      | expense  | 0.10 | `41` | taxable  | G11 |
| EXP_HOME_OFFICE  | expense  | 0.10 | `41` | taxable  | G11 |
| EXP_INSURANCE    | expense  | 0.10 | `41` | taxable  | G11 |
| EXP_PROFESSIONAL | expense  | 0.10 | `41` | taxable  | G11 |
| EXP_MATERIALS    | expense  | 0.10 | `41` | taxable  | G11 |
| EXP_SOFTWARE     | expense  | 0.10 | `41` | taxable  | G11 |
| EXP_TELCO        | expense  | 0.10 | `41` | taxable  | G11 |
| EXP_SUPER        | expense  | 0.00 | `32` | gst_free | Super contributions — no GST per ATO |
| EXP_TRAINING     | expense  | 0.10 | `41` | taxable  | G11 |
| EXP_TOOLS        | expense  | 0.10 | `41` | taxable  | G11 |
| EXP_TRAVEL       | expense  | 0.10 | `41` | taxable  | G11 (domestic only — international fares are GST-free; flagged for tenant override) |
| EXP_BANK         | expense  | 0.00 | `32` | input_taxed | Bank fees are typically input-taxed |
| EXP_OTHER        | expense  | 0.10 | `41` | taxable  | G11 |
| CAP_PURCHASE     | expense  | 0.10 | **needs new code** | capital  | G10 — see below |
| PER_DRAWINGS     | expense  | 0.00 | None     | n/a      | Drawings aren't a P&L line; no tax_code |
| TX_TRANSFER      | transfer | 0.00 | None     | n/a      | Bank transfer; not a P&L line |

**Two issues this table surfaces:**

1. **`CAP_PURCHASE` needs a capital-acquisitions tax_code.** Today primary's CoA doesn't have one. Either:
   - (a) Add `43 GST on capital acquisitions` (rate 10, reporting_type `capital`) to the AU seed and migrate existing tenants to include it. Existing CAP_PURCHASE rows are zero on primary (cashbook isn't its primary mode) so the migration is low-risk.
   - (b) Leave CAP_PURCHASE without a tax_code and document "capital purchases via cashbook do not yet flow to G10". G10 stays at 0 for cashbook tenants.

   **Recommendation: (a).** It's a one-line seed addition plus an alembic migration that idempotently inserts the row for each company that already has `33` + `41`. Untouched if the tenant added their own capital code.

2. **`EXP_BANK` should be `input_taxed`** per ATO treatment of financial supplies. Sauer's tax_codes don't have an `input_taxed` reporting_type today. Either:
   - (a) Add `44 Input taxed` (rate 0, reporting_type `input_taxed`) to the seed
   - (b) Fall back to `32 GST free` which is wrong-but-close (no GST claim either way, but G3 vs neither is the diff)

   **Recommendation: (a) again**, paired with the capital code addition.

### Resolver function

In `services/cashbook.py`:

```python
async def _resolve_category_tax_code(
    session: AsyncSession,
    company_id: uuid.UUID,
    category: CashbookCategory,
) -> uuid.UUID | None:
    """Resolve the cashbook category's tax_code field to a TaxCode.id
    in this company. Falls back to reporting_type match if the named
    code isn't present. Returns None if neither resolves (logged
    warning; the line will still post but BAS aggregation will miss
    it).
    """
    if category.tax_code is None:
        return None
    # 1. Exact code match
    tc = await session.execute(
        select(TaxCode).where(
            TaxCode.company_id == company_id,
            TaxCode.code == category.tax_code,
            TaxCode.archived_at.is_(None),
        )
    )
    row = tc.scalars().first()
    if row is not None:
        return row.id
    # 2. Reporting-type fallback
    tc = await session.execute(
        select(TaxCode).where(
            TaxCode.company_id == company_id,
            TaxCode.reporting_type == category.reporting_type,
            TaxCode.archived_at.is_(None),
        ).order_by(TaxCode.code).limit(1)
    )
    row = tc.scalars().first()
    if row is not None:
        logger.warning(
            "cashbook category %s expected tax_code %r in company %s, "
            "fell back to %r [reporting_type=%s]",
            category.code, category.tax_code, company_id,
            row.code, row.reporting_type,
        )
        return row.id
    # 3. No match at all — return None and let BAS aggregator skip
    logger.warning(
        "cashbook category %s: no tax_code or reporting_type=%s found "
        "in company %s — JE line will post with NULL tax_code_id and "
        "be invisible to BAS",
        category.code, category.reporting_type, company_id,
    )
    return None
```

Called once per cashbook entry, result threaded into both lines (the category line and, for income, the bank counter-line where applicable).

### Migration

```
0141_add_capital_input_taxed_tax_codes.py
```

For each `company_id` that already has both `33` and `41`:
1. INSERT `43 GST on capital acquisitions` (rate 10, taxable rep_type — wait, the rep_type column accepts free text; use `capital`)
2. INSERT `44 Input taxed` (rate 0, reporting_type `input_taxed`)

Idempotent (`ON CONFLICT DO NOTHING` via the existing `uq_tax_codes_company_code_active` index).

## Estimated effort

- Schema work + seed update: 1 hour
- `CashbookCategory` dataclass + 20 mappings: 1 hour
- Resolver function + plumb into `record_cashbook_entry`: 1 hour
- Migration 0141: 1 hour
- Tests (one per category code rolling up correctly into BAS): 3-4 hours
- BAS aggregator review (does it actually pick up `capital` and `input_taxed` reporting_types correctly?): 1-2 hours

**Total: ~8-10 hours.**

## Open questions for Richard

1. **Tenant override for category → tax_code mapping**: do we expose a UI for tenants to amend the default mapping (e.g. a bookkeeper who categorises bank fees as "GST free" not "input taxed")? Recommendation: yes, but in v2. v1 ships with hard-coded defaults; tenants who disagree open a support ticket.

2. **`CAP_PURCHASE` UI experience**: the current cashbook UI doesn't ask "is this a capital purchase?". Should CAP_PURCHASE be promoted to a separate UI affordance (e.g. a "capital" checkbox on every expense)? Recommendation: leave as-is for v1 (CAP_PURCHASE stays a category); revisit when fixed-assets module lands properly.

3. **International travel + fares treatment**: EXP_TRAVEL currently 10% GST default. International fares are GST-free. Recommendation: leave it as default-taxable, let the user override on entry (the cashbook entry form already accepts an explicit `gst_amount` override that overrides the category default).

4. **Backfill existing cashbook entries**: production `journal_lines` already have NULL `tax_code_id` for all cashbook-originated lines. Should we backfill via a migration based on the source entry's category? Recommendation: **yes** — write a small one-shot script that joins `journal_entries.attachments->'cashbook_meta'->>'category_code'` to find each cashbook-originated line and stamp it. ~30 minutes to write; safe because it's a single-column UPDATE.
