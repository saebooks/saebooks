# SAE Books — Cashbook edition (single-entry for sole traders)

Status: design doc, draft 1.

## Implementation status

| Phase | Scope | Commit | Tests |
|---|---|---|---|
| **A** | Schema migration 0093, `record_cashbook_entry` service, default category dataclass, idempotency, period locks, GST. | `088b4eb` | 15 service ✓ |
| **Gate 1** | AU sole-trader category signoff. | `e688a43` | n/a |
| **B** | `/api/v1/cashbook` POST/GET/list/categories/summary routes. | `8f5b29d` | 19 API ✓ |
| **B.5** | `PATCH` (void-and-recreate) + `DELETE` (soft-delete via reversal JE), idempotent. | `2b150cd` | 5 service + 7 API ✓ |
| **C** | `POST /setup` onboarding, `POST /upgrade-to-full` one-way migration, `bookkeeping_mode` + `cashbook_default_bank_account_id` in `CompanyOut` for menu gating. | `5723fc6` | 7 API ✓ |
| **D** | End-to-end happy-path test (setup → entries → upgrade), this status table. | `a873036` | 1 e2e ✓ |
| **Gate 2** | Mode-switch UX review. | pending — needs `saebooks-web` templates |
| **Out of scope (v1)** | TX_TRANSFER endpoint (needs two-bank-account flow); SISS bank-feed wiring; mobile-app shell. | — | — |

Total tests: **54 passing** (20 service + 33 API contract + 1 e2e).

Outstanding follow-ups for Gate 2 / next iteration:

- `saebooks-web` cashbook templates (mobile-first list + entry form per §7).
- `TX_TRANSFER` endpoint (two-bank flow, P&L-neutral).
- Receipt attachments via the Phase 1 vault (`entity_type='cashbook'`).
- BAS prep (G1/G3/1A/1B) projection — uses the existing BAS calc engine.

## 1. Problem and target user

The Australian sole trader has been treated as a second-class citizen by every
serious accounting product on the market. They sit below the $75k GST
registration threshold most years, they don't have employees, they don't issue
formal invoices half the time, and they don't want to learn what "debit" and
"credit" mean. They want three numbers: what came in, what went out, what's
left. They live on their phone. They hate accountants. They will abandon a
product the first time it shows them a balance sheet.

Competitors: Hnry (clean, but locks customer into a managed-service model
where Hnry holds the bank account), Solo and Rounded (nice, feature-thin),
QBO Self-Employed (legacy bones show through the moment you scroll), Wave.
Xero doesn't really compete here.

Why does SAE Books care about a category hostile to its core ledger model?

1. **Top of funnel.** A sole trader who passes $75k, takes on a contractor,
   or incorporates needs a real ledger. If they're on SAE Books in cashbook
   mode, the upgrade is a flag flip. If they're on Hnry, we never see them.
2. **Defensive moat.** Cede sole traders and we hand competitors the funnel.
3. **Volume.** ABS counts ~1.5M sole traders vs ~2.5M SMBs in AU. Acquisition
   cost on the cashbook end is an order of magnitude lower than full edition,
   where the buying decision involves an accountant.

Hard rule: cashbook exists to *feed* full edition and to be credible
standalone. It does NOT exist to dilute the marketing position — see §2.

## 2. The architectural insight

The product north-star says the API is the licensed product and the API
schema is strictly accounting. Cashbook edition does not break that. It is a
**UI mode**, not a parallel ledger. Every cashbook entry compiles down to a
real `JournalEntry` with two `JournalLine` rows, and every cashbook report is
a slice of the existing P&L queries.

The mapping is mechanical:

- Income entry of $X → DR Bank $X, CR Income (chosen category) $X
- Expense entry of $X → DR Expense (chosen category) $X, CR Bank $X

The "Bank" account is the implicit counter-account, configured per company as
`cashbook_default_bank_account_id`. The "category" is whichever expense or
income account the user picked in the picker. Everything else — period locks,
RLS, audit log, immutability rules, archive semantics — comes for free.

This matters: (a) the ledger stays pure — future SISS feeds match the
auto-generated JE without caring about mode; (b) upgrade is trivial — every
historic cashbook entry is *already* a real JE in the right accounts;
(c) marketing position holds — one ledger, one UI mode hiding it, no
parallel "single-entry storage" engine sitting alongside the journal.

The risk is the inverse — that cashbook UX leaks accounting jargon.
Mitigate by treating the mode as a *strict subset* of full-edition surface,
never its own surface. A picker that says "Materials" targets account
`5210 - Materials` under the hood; the user never sees the code.

## 3. Schema changes

Three columns on `companies`. No new tables for v1.

```sql
ALTER TABLE companies
  ADD COLUMN bookkeeping_mode VARCHAR(16) NOT NULL DEFAULT 'full',
  ADD COLUMN cashbook_default_bank_account_id UUID
    REFERENCES accounts(id) ON DELETE RESTRICT,
  ADD COLUMN cashbook_categories JSONB;

ALTER TABLE companies
  ADD CONSTRAINT ck_cashbook_requires_bank
    CHECK (bookkeeping_mode <> 'cashbook'
           OR cashbook_default_bank_account_id IS NOT NULL);
```

`bookkeeping_mode` is a Postgres enum-via-VARCHAR (consistent with existing
patterns like `EntryStatus`) with values `'full' | 'cashbook'`. Default is
`'full'`, so every existing company is unaffected by the migration.

`cashbook_default_bank_account_id` is the implicit counter-account. The CHECK
constraint refuses to leave a company in `cashbook` mode without one set —
this fails closed.

`cashbook_categories` is JSONB holding per-company overrides to the default
list (label rename, hide a category, repoint to a different account).
Defaults live in code. I rejected a separate `cashbook_categories` table:
~30 default rows, almost no company will override more than a handful, and
JSONB lets us version defaults in code without per-customer migrations.
Revisit when average override count exceeds five.

The shape inside `cashbook_categories`:

```json
{
  "version": 1,
  "overrides": {
    "EXP_VEHICLE":  { "label": "Ute & fuel", "account_id": "..." },
    "INC_INTEREST": { "hidden": true }
  }
}
```

If `cashbook_categories` is NULL the company gets the bare defaults. The
resolver merges the defaults with the overrides at read time.

No change to `journal_entries`, `journal_lines`, `accounts`. The product
schema stays put. The cashbook is purely additive on `companies`.

## 4. Auto-journal generator

The whole cashbook UI sits on top of one service function. Tight surface,
small blast radius.

```python
async def record_cashbook_entry(
    *,
    db: AsyncSession,
    tenant_id: UUID,
    company_id: UUID,
    entry_date: date,
    description: str,
    amount: Decimal,                      # gross, always positive
    direction: Literal["income", "expense"],
    category_account_id: UUID,
    gst_amount: Decimal | None = None,    # None = use category default
    idempotency_key: str,                 # required for retry safety
    actor: str,
) -> JournalEntry: ...
```

For an expense of $110.00 (GST inclusive, registered) categorised as
Materials, with bank `1100 - Westpac Business`:

```
JournalEntry  ref=CB-000123  date=2026-05-08  description="Bunnings — drill bits"  status=POSTED
  Line 1  account=5210 Materials       debit=100.00  credit=  0.00  gst_amount=10.00  tax_code=GST
  Line 2  account=2410 GST Paid        debit= 10.00  credit=  0.00
  Line 3  account=1100 Westpac Bus.    debit=  0.00  credit=110.00
```

For a non-registered trader the GST line is omitted; the JE is a clean
two-line entry. GST default per category is read from the category table;
override per-entry is allowed.

**Idempotency.** Same `idempotency_key` returns the same `JournalEntry.id`,
period. Implementation uses the existing `idempotency_key` table (already
serving JE post/reverse) keyed by `(tenant_id, company_id, scope='cashbook',
key)`. Replays return the stored response unchanged.

**Missing default bank.** If `bookkeeping_mode='cashbook'` and
`cashbook_default_bank_account_id` is unset, the function raises a typed
error (`CashbookNotConfigured`) with `{"code": "cashbook_no_default_bank"}`
that the UI surfaces as a one-click "pick your bank account" prompt. The
CHECK constraint above means a properly-onboarded company can never reach
this state; the error exists for partial migrations and tests.

**Multi-currency.** Out of scope for v1. Sole traders in AUD only. The
function asserts `company.base_currency == 'AUD'` and refuses otherwise.
Multi-currency cashbook is a 2027+ problem, if ever — and a sole trader
billing USD almost certainly has crossed into "needs full edition" territory.

**Period locks.** The function honours `period_locks` exactly as full-edition
JE creation does: a cashbook entry dated before the lock cutoff is rejected
with the existing 422 response. Same code path, same error.

## 5. Category model

Categories are a fixed taxonomy mapping to the chart of accounts. The user
picks one per entry; the cashbook never asks "which account?". Draft starting
list, ~30 entries grouped by tax treatment. **This is Gate 1 — Richard signs
off on the mapping for AU tax correctness before any code lands.**

| Code | Label | Group | Default account | GST default | Notes |
|---|---|---|---|---|---|
| INC_SALES | Sales | Income | 4000 Sales | 10% | Goods sold |
| INC_SERVICES | Services | Income | 4100 Service revenue | 10% | Labour billed |
| INC_INTEREST | Interest received | Income | 4900 Interest income | 0% | GST-free |
| INC_OTHER | Other income | Income | 4990 Other income | 10% | |
| EXP_VEHICLE | Vehicle & fuel | Vehicle | 5310 Motor vehicle | 10% | Logbook hint |
| EXP_HOME_OFFICE | Home office | Home office | 5320 Home office | 10% | sqm/% reminder |
| EXP_INSURANCE | Insurance | Insurance | 5410 Insurance | 10% | |
| EXP_PROFESSIONAL | Accounting & legal | Professional | 5420 Professional fees | 10% | |
| EXP_MATERIALS | Materials & supplies | Materials | 5210 Materials | 10% | |
| EXP_SOFTWARE | Software & subscriptions | Software | 5430 Software | 10% | |
| EXP_TELCO | Phone & internet | Telco | 5440 Telco | 10% | |
| EXP_SUPER | Personal super contributions | Super | 5510 Super | 0% | Notice-of-intent reminder |
| EXP_TRAINING | Training & courses | Training | 5450 Training | 10% | |
| EXP_TOOLS | Tools (under $300) | Tools | 5220 Small tools | 10% | Under instant write-off |
| EXP_TRAVEL | Travel | Travel | 5460 Travel | 10% | |
| EXP_BANK | Bank fees | Bank | 5470 Bank charges | 0% | GST-free |
| EXP_OTHER | Other expense | Other | 5990 Other expense | 10% | |
| CAP_PURCHASE | Capital purchase (>$300) | Capital | 1500 Plant & equipment | 10% | Triggers "add to asset register" prompt — see §9 |
| PER_DRAWINGS | Drawings (personal use) | Personal | 3500 Drawings | 0% | Not deductible; flagged in BAS prep |
| TX_TRANSFER | Transfer between bank accounts | Transfer | (special — second bank) | 0% | Excluded from P&L; see note |

Each row in code is a dataclass with `code`, `label`, `group`,
`default_account_code`, `tax_treatment`, `gst_default`, `hint_text`. Account
resolution is by code-lookup at runtime so the same default list works
against any seeded chart of accounts.

`TX_TRANSFER` is the one special case: two bank accounts, P&L-neutral.
Recommendation: separate "Transfer" button on the entry form, distinct from
"Money in / Money out". Mixing it into the income/expense flow confuses
everyone.

The list is intentionally short, and omits "Cost of Goods Sold" (implies
inventory) and "Wages" (implies employees). Anyone needing those has
crossed into full edition.

## 6. API surface

All endpoints under `/api/v1/cashbook/`, Bearer auth, tenant + company scoped
identical to existing v1 routers, dependencies `[require_bearer,
get_active_company_id]`.

```
POST   /api/v1/cashbook/entries                 # idempotent, X-Idempotency-Key required
GET    /api/v1/cashbook/entries?from=&to=&category=&direction=&limit=&cursor=
GET    /api/v1/cashbook/entries/{id}
PATCH  /api/v1/cashbook/entries/{id}            # void & re-create, atomic
DELETE /api/v1/cashbook/entries/{id}            # void (reverses JE)
GET    /api/v1/cashbook/categories
GET    /api/v1/cashbook/summary?from=&to=
```

POST request:

```json
{
  "entry_date": "2026-05-08",
  "description": "Bunnings — drill bits",
  "amount": "110.00",
  "direction": "expense",
  "category_code": "EXP_MATERIALS",
  "gst_amount": "10.00"
}
```

POST response (201):

```json
{
  "id": "8d3f...",
  "ref": "CB-000123",
  "entry_date": "2026-05-08",
  "description": "Bunnings — drill bits",
  "amount": "110.00",
  "direction": "expense",
  "category_code": "EXP_MATERIALS",
  "category_label": "Materials & supplies",
  "gst_amount": "10.00",
  "journal_entry_id": "0a91...",
  "journal_entry_ref": "JE-2026-000456",
  "version": 1
}
```

The cashbook entry is a **view** of a JE — there is no separate
`cashbook_entries` table. The GET endpoint reads `journal_entries` filtered
by `attachments->>'cashbook_meta' IS NOT NULL` (the generator stamps a
`cashbook_meta` blob into `JournalEntry.attachments` so reads can reconstruct
the cashbook view without parsing line shapes). Alternative is a
`cashbook_entries` view-table — open question, see §10.

`PATCH` and `DELETE`: **void & re-create**, never in-place. PATCH reverses
the existing JE and posts a fresh one. DELETE reverses without re-creating.
Audit purity at the cost of more rows — fine at sole-trader volume (dozens
per month, not thousands). Side benefit: undo is cheap to wire later.

`GET /summary` returns a P&L-shaped summary collapsed into cashbook
categories:

```json
{
  "from": "2026-04-01",
  "to":   "2026-06-30",
  "income_total":   "8420.00",
  "expense_total":  "3115.40",
  "net":            "5304.60",
  "by_category": [
    {"code":"INC_SERVICES","label":"Services","amount":"7800.00","count":12},
    {"code":"INC_OTHER","label":"Other income","amount":"620.00","count":2},
    {"code":"EXP_MATERIALS","label":"Materials & supplies","amount":"1340.00","count":8}
  ],
  "gst_collected": "766.00",
  "gst_paid":      "283.20"
}
```

The summary is a projection over the existing `journal_lines` query, scoped
to the company and grouped by the cashbook category derived from
`account_id`. No new aggregation engine.

## 7. UX shape

Mobile first. Phone is the primary form factor; desktop is fine-but-not-the-
target. The big idea is one screen — the **list** — and one form. Everything
else is a settings drawer.

```
+------------------------------+
|  May 2026   ·  $5,304 net    |   <- summary header, always visible
|  in $8,420   out $3,115      |
+------------------------------+
| 08 May  Bunnings drill bits  |
|         Materials   -$110.00 |
| 06 May  Inv 1043 — ABC Ltd   |
|         Services   +$2,200.00|
| 05 May  Optus mobile         |
|         Telco        -$95.00 |
|              ...             |
|                              |
|                       (  +  )|   <- big plus, fixed bottom-right
+------------------------------+
```

The plus opens a single full-screen form:

```
[ Today  ▾ ]                       <- date, defaults to today
( In )( OUT )                      <- direction toggle
[ $              ]                 <- amount, big numeric pad
[ Materials   ▾ ]                  <- category, recently-used at top
[ Bunnings — drill bits ]          <- description, optional
[ +Attach receipt ]                <- camera or file (Phase 1 vault)
[ Save ]
```

Hide every full-edition menu item when in cashbook mode: chart of accounts,
journal entries, invoices, bills, balance sheet, depreciation, fixed assets,
recurring transactions, the lot. The user sees: list, summary, categories
(read-only), settings, BAS prep (if registered).

Hnry is the model: clean, opinionated, one screen. QBO-SE has QBO's
information density without its controls — worst of both. Lean Hnry-style.

## 8. Mode switching and migration

**Onboarding.** Signup adds a single radio: "Are you a sole trader?" If yes,
flip `bookkeeping_mode='cashbook'`, prompt for the user's bank (creates a
default bank account if they don't have one already, sets
`cashbook_default_bank_account_id`), seed the default category overrides as
NULL, hide full menus. If no, behaviour is exactly as today.

**Cashbook → full upgrade.** One-way. UI shows a banner "outgrown the
cashbook? Switch to full edition" → confirmation → flag flip. All previously-
entered cashbook entries remain valid because they were always real journal
entries. The full-edition menu items appear; the cashbook menu remains as a
"Quick entry" option for users who like it.

**Full → cashbook downgrade.** Not supported. A full-edition company has
journal entries, invoices, bills, payroll runs and so on that don't fit the
cashbook UI's category model. Pretending to downgrade would either hide
those records (data loss in practice) or render incoherently. Refuse at the
API level; the UI doesn't even expose the option.

**Existing customers.** Untouched. Default for new and existing companies is
`'full'`. The migration adds the columns with the default value and a NULL
override; nobody who isn't deliberately switching ever sees a difference.

## 9. What NOT to build (yet)

- **Bank-feed integration.** Cashbook v1 is manual entry. SISS bank feeds
  are still in onboarding (see [[bank-feeds]]); when they land, cashbook
  becomes the natural surface for them. Until then, manual entry only.
- **Multi-currency.** AUD only. Sole traders billing in USD have outgrown
  cashbook by the second invoice.
- **Multi-user companies in cashbook mode.** Sole traders are single-user by
  definition. Two-user cashbook would invite "bookkeeper + business owner"
  use cases that need full edition's permission model.
- **Invoicing.** Cashbook records money received, not invoices issued. If a
  user needs to send an invoice, they upgrade. The marketing copy must be
  honest about this: cashbook is *records of money*, not a business OS.
- **Asset register / depreciation.** Capital purchase >$300 surfaces a flag
  in the form ("This looks like a capital purchase. Add it to your asset
  register?") that links to a full-edition upgrade. Don't try to do
  depreciation in cashbook UI.
- **Payroll / STP.** No employees by definition.
- **Forecasting / cash flow projections.** Tempting and out of scope for v1.

## 10. Open questions

- **GST-registered sole trader below threshold.** Voluntary registration is
  legal and not uncommon (allows GST claims on inputs). Cashbook needs the
  existing `companies.gst_registered` flag to drive whether the GST line is
  generated. Easy. The harder question is BAS prep — see below.
- **Receipt attachments.** Phase 1 saebooks-vault is the obvious hook
  (`entity_type='cashbook'`). Recommendation: re-use the vault — we just
  built it and cashbook is exactly the surface it was designed for.
- **Reporting depth.** P&L only in v1; add BAS prep (G1/G3/1A/1B) in v1.1
  after a real user has lodged a quarter manually. BAS calc engine already
  exists; it's a UI question.
- **Pricing.** Free / cheaper / same? Marketing call. Instinct: cheap,
  near-free to maximise funnel, with paid receipt-vault + BAS-prep add-ons
  for unit economics. Defer to Richard.
- **Mobile app vs PWA.** PWA suffices for v1. Native is a 2027 question
  driven by offline-first.
- **Storage shape.** Recommended: JE-only with a `cashbook_meta` JSONB blob
  on `JournalEntry.attachments`. Promote to a `cashbook_entries` view-table
  the moment we want a second non-JE field.

## Gates

- **Gate 1.** Category list (§5). Richard signs off on AU tax correctness
  before any code lands. If the mapping is wrong every customer gets wrong
  P&L and we break BAS. This is the biggest single risk in the design.
- **Gate 2.** Mode-switch UX (§8). Onboarding flow needs a Richard-eyes pass
  because it's the first thing a new sole trader sees, and the
  "no downgrade" rule needs to be communicated clearly without scaring
  people off.

What's queued for Phase A: schema migration, `record_cashbook_entry` service
function with idempotency + period locks + GST tests, default category
dataclass, POST + GET + summary endpoints behind a `cashbook` feature flag,
RLS spot-check that cashbook reads can't escape the active company. UI work
(saebooks-web cashbook templates, mobile layout, mode-aware menu hiding)
follows in Phase B.
