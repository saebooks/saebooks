# Cashbook Gate 1 — AU sole-trader category signoff

Status: **awaiting Richard's signoff** before Phase B routes are exposed in production.
Authors: Claude (Opus 1M, autonomous overnight build). Date: 2026-05-08.
Companion to: `docs/cashbook-edition-design.md`.

## What this is

The cashbook UI hides the chart of accounts from sole traders behind a fixed
picker of ~20 categories. Every cashbook entry → real `JournalEntry` whose
account_id resolves through this table. **Get the mapping wrong and every
customer gets wrong P&L and broken BAS.** This is the single biggest risk in
the cashbook design — hence the gate.

The code lives at `saebooks/services/cashbook_categories.py`. Defaults
resolve against the AU CoA seed at `saebooks/seed/au/account.account-au.csv`
(loaded by `seed/load_au_coa.py`). Per-company overrides via
`companies.cashbook_categories` JSONB are the escape hatch.

## What you need to decide

Three questions, in order of importance:

### 1. Are the GST defaults correct for AU sole traders?

GST default is the rate baked into the picker for that category. The cashbook
service generates a GST line iff `company.gst_registered = True` AND
`category.gst_default > 0`. A non-registered trader never generates GST lines,
no matter the category default.

| Code | Label | GST default | Reason |
|------|-------|-------------|--------|
| INC_SALES | Sales | 10% | Default GST-able sale |
| INC_SERVICES | Services | 10% | Default GST-able service |
| INC_INTEREST | Interest received | **0%** | Input-taxed financial supply (s.40-5 GST Act) |
| INC_OTHER | Other income | 10% | Conservative — user can override |
| EXP_VEHICLE | Vehicle & fuel | 10% | Most fuel + vehicle costs GST-bearing |
| EXP_HOME_OFFICE | Home office | 10% | |
| EXP_INSURANCE | Insurance | 10% | Most business insurance is GST-able. Stamp duty portion is GST-free — the user splits if precise |
| EXP_PROFESSIONAL | Accounting & legal | 10% | |
| EXP_MATERIALS | Materials & supplies | 10% | |
| EXP_SOFTWARE | Software & subscriptions | 10% | Australian-supplied software → GST. Overseas SaaS now usually has GST charged via offshore-supply rules; conservative default = 10% |
| EXP_TELCO | Phone & internet | 10% | |
| EXP_SUPER | Personal super contributions | **0%** | Super is GST-free (s.38-355) |
| EXP_TRAINING | Training & courses | 10% | Most commercial training is GST-bearing. TAFE/uni → GST-free. |
| EXP_TOOLS | Tools (under $300) | 10% | |
| EXP_TRAVEL | Travel | 10% | Domestic travel is GST. International airfares are GST-free — open question if we should split |
| EXP_BANK | Bank fees | **0%** | Bank charges are input-taxed (s.40-5) |
| EXP_OTHER | Other expense | 10% | Conservative |
| CAP_PURCHASE | Capital purchase (>$300) | 10% | |
| PER_DRAWINGS | Drawings (personal use) | **0%** | Not deductible, not a supply |
| TX_TRANSFER | Transfer between accounts | 0% | P&L-neutral |

**Open questions for you:**

- **EXP_TRAVEL.** Should we split into `EXP_TRAVEL_DOM` (10%) and
  `EXP_TRAVEL_INTL` (0%)? Hnry doesn't bother — they let the user mark
  per-entry. I lean: leave as 10% default with a hint, override per-entry.
  Adds picker friction otherwise.
- **EXP_TRAINING.** Same question. TAFE/uni courses are GST-free, commercial
  training is GST-bearing. Lean: 10% default with hint.
- **EXP_INSURANCE.** Stamp duty portion isn't GST-able. Most practitioners
  net it: claim 10% on the full premium, accept the small overstatement.
  Defensible. Leave at 10%.

### 2. The 4 GAP categories — extend the seed CoA, or live with the placeholders?

The AU Odoo CoA we seed (`account.account-au.csv`) is trade-focused and
missing four lines a sole trader actually needs. I've pointed the cashbook
categories at the closest existing account, marked each in code as `# GAP:`,
and left per-company override as the workaround. Recommendation in
parentheses.

| Cashbook category | Currently maps to | Should map to (proposed) |
|---|---|---|
| INC_SERVICES | 4-2000 Wholesale Sales | **NEW: 4-1500 Service Revenue** *(recommend)* |
| EXP_SOFTWARE | 6-2300 Office Expenses | **NEW: 6-1850 Software & Subscriptions** *(recommend)* |
| EXP_TRAINING | 6-2450 Other Employer Expenses | **NEW: 6-2470 Training & Development** *(recommend — and "employer" framing is wrong for sole trader)* |
| EXP_BANK | 6-1930 Other Interest | **NEW: 6-1940 Bank Fees & Charges** *(recommend — interest framing is wrong)* |

Why I recommend extending the seed: every sole trader has every one of these.
Reporting and BAS prep will show them under the wrong heading, which trips
the user the first time they check their P&L. The cost of extending the seed
is one CSV edit + one targeted migration to backfill new accounts into
existing companies — half a day.

If you decide **don't extend**, leave the placeholders and document the
mapping in the public marketing copy + onboarding "your accountant might want
to repoint these" note. Per-company override exists for the fastidious.

### 3. Categories I omitted on purpose — ratify or push back

Five things you'd expect to see on a sole-trader picker that I left off:

| Omitted | Why |
|---|---|
| **Cost of Goods Sold** | Implies inventory; sole trader using inventory is on the wrong edition. If they tag goods as "Materials" the P&L still works, BAS still works. |
| **Wages / payroll** | Sole traders by definition don't have employees in cashbook scope. Anyone needing wages → full edition. |
| **Rent (commercial)** | Rare for sole traders; rolls into EXP_HOME_OFFICE for the common case (working from home) or EXP_OTHER for the rare commercial-lease case. Extending if a single customer asks. |
| **Internet (separate from phone)** | Combined with EXP_TELCO. Splitting would force a category decision in the picker every time, which violates the "one screen, fast entry" principle. |
| **Charity / donations** | Out of policy. Donations need a DGR test before they're deductible; not safe to default. User logs as EXP_OTHER. |

**Push back if:** any of these feels wrong for the sole-trader profile we're
targeting. I'm specifically nervous about Rent (commercial) — if you've seen
many sole traders with a commercial space, we should add it.

## What this means for code

If you sign off as-is (option A), nothing changes — the code already does it.

If you decide to extend the seed (option B, recommended), I'll:

1. Add the four new accounts to `seed/au/account.account-au.csv`:
   - `4-1500 Service Revenue` (income)
   - `6-1850 Software & Subscriptions` (expense)
   - `6-2470 Training & Development` (expense)
   - `6-1940 Bank Fees & Charges` (expense)
2. Add a new alembic migration that backfills these accounts into every
   existing company that already has the base AU CoA loaded — so existing
   tenants pick up the new lines on the next deploy.
3. Update `saebooks/services/cashbook_categories.py` to point at the new
   codes. Remove the `# GAP:` comments.
4. Refresh the Phase A tests; the public-API behaviour doesn't change.

Time: half a day on top of Phase B.

If you want any of the GST defaults changed (option C), tell me which and
I'll patch the dataclass + tests in a single commit. Five-minute turnaround.

## What's blocked on this gate

- Phase B (routes + templates) can build against the current category list as
  a contract — gate on category-mapping doesn't block route work.
- Phase C (onboarding flow) can build, but the marketing/onboarding copy that
  references categories is blocked.
- **Production rollout (any phase) is blocked.** Don't expose cashbook to
  real customers until the mapping is signed off — wrong P&L is the
  worst-possible bug for an accounting product's reputation.

## Recommended decision

**Option B + signoff on GST table as-is + ratify the omissions.** Half a day
to extend the seed; everything else stays.

If you're in a hurry: **Option A.** Roll with placeholders, ship Phase B for
internal testing, decide on the seed extension after the first real cashbook
customer hits a P&L surprise.

What I need from you to unblock: a one-line answer — "A", "B" or "C with
these changes: …".
