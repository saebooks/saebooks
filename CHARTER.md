# SAE Books — Project Charter

> *Your books. Your database. Your control.*

**Status:** Private development, not yet public. Private until launch.
**Owner:** Richard Sauer, SAE Engineering.
**Started:** 2026-04-15.

This charter captures the strategic decisions that shape SAE Books. It is
the senior document in the repository — `SPEC.md` (technical) and every
future engineering doc is subordinate to it. When a code decision conflicts
with this charter, the charter wins and the code is wrong.

Amendments to this charter require an explicit git commit updating this
file. No implicit drift.

---

## 1. What SAE Books is

A self-hosted, double-entry accounting system for small-to-medium
businesses who want their financial data on their own infrastructure.

- **Core scope:** chart of accounts, general ledger, sales (income),
  purchases (expenses), contacts, banking, reconcile, GST/BAS reporting,
  standard reports.
- **Out of scope for v1:** payroll / STP / super (no ATO DSP certification
  pit), live bank feeds are v1.1, inventory belongs in a sibling
  project.
- **In scope for v1, schema-only:** **multi-market readiness.** The
  v1 schema and API surface are designed so that adding non-AU
  jurisdictions (UK, US, EU member states) is a localisation-pack
  exercise (§6.15) rather than a refactor. Multi-currency, address
  polymorphism, bank-account polymorphism, and a pluggable tax-engine
  registry are present from the start. UI, BAS-equivalent reports, and
  certified e-lodgement engines for non-AU jurisdictions ship later.
- **Data model inspiration:** MYOB AccountRight Classic v19 (reverse-
  engineered from its ODBC schema).
- **UX inspiration:** the same — MYOB v19 Command Centre, keyboard-
  driven, desktop-density, built for people who do this every week.

## 2. The one sentence

> *Your books. Your database. Your control.*

Everything else in this charter either serves that sentence or gets cut.

## 3. Why this exists

Two reasons, in this order:

1. **Richard needs to get off QBO.** He doesn't love it, doesn't trust
   cloud lock-in, and Australia has no open-source option that's both
   compliant and usable for a one-person Pty Ltd. Building it for himself
   is the first goal — succeed or fail, the primary user is already
   committed.
2. **The market gap is real.** Odoo is too ERP-shaped. GnuCash is too
   hobbyist. Xero and QBO are subscription-locked cloud-only. MYOB's v19
   is read-only from 28 Feb 2026. A local-first, open-source, AU-
   compliant bookkeeping app with good UX is a hole in the market that
   nobody's currently filling.

If we build the first thing well, the second thing follows. If the second
thing never happens, the first thing still justifies the effort.

## 4. Positioning / hero promise

**"Your books. Your database. Your control."**

Concretely:

- Your data lives in a Postgres database you control — local machine,
  your own server, cloud VM you rent, wherever. Not ours.
- The core app works fully offline. Internet-dependent features (bank
  feeds, FX rates, ABN lookup, future BAS e-submission) are additive,
  never required.
- Full data export in open formats (CSV, JSON, OFX, QIF) is a
  first-class feature, not an afterthought — user trust requires
  genuine portability.
- The container runs anywhere Docker runs: your NAS, a Raspberry Pi,
  a laptop, a VPS, our hosted offering. Same code.

This is the single largest differentiator from every cloud-only
incumbent, and it costs nothing to promise — we just have to not build
it wrong.

## 5. Licence and IP

- **Core code:** AGPLv3 + commercial dual licence. AGPLv3's
  network-copyleft is strong enough that commercial forks can't run a
  modified SAE Books as a proprietary SaaS without publishing their
  modifications. Customers who want to use SAE Books outside AGPL's
  terms (typical: integrators bundling SAE Books in a proprietary
  product, or paying subscribers on Business / Pro / Enterprise tiers)
  buy a commercial licence. Top-level summary in `LICENSING.md`;
  customer-facing detail in `SPEC-LICENSING.md`; legal text in
  `LICENSE`.
- **Repos covered:** `saebooks` (engine) and `saebooks-web` (server-
  rendered frontend) are both under AGPLv3 + commercial dual licence.
  The frontend is open because community traction depends on the
  community being able to read, audit, and modify the screens they
  use every day. Hosted SaaS infrastructure (billing portal,
  onboarding, ops tooling) and certified e-lodgement engines (ATO
  SBR, HMRC MTD, IRS e-file, etc.) are proprietary, never published.
- **Localisation packs:** see §6.15. Country charts of accounts, tax
  codes, and report templates are AGPL and community-maintainable;
  certified e-lodgement engines per jurisdiction are paid commercial.
- **Trademark:** "SAE Books," "saebooks," and the SAE Books logo are
  trademarks of SAE Engineering. Forking the source under AGPL is
  fine; calling the fork "SAE Books" is not. Full policy in
  `TRADEMARK.md`. The trademark is the brand-protection lever AGPL
  doesn't provide — necessary for the "users can trust the name"
  promise once external forks exist.
- **CLA (Contributor License Agreement):** required from every external
  contributor starting from the first pull request, covering all SAE
  Books repositories under one signature. Gives SAE Engineering the
  right to re-license contributions under a commercial licence in
  addition to AGPL. Modelled on the Apache ICLA v2.2. Full text in
  `CLA.md`. Automated via the CLA Assistant bot once the repo is
  public.
- **Copyright:** © 2026 Richard Sauer / SAE Engineering. Every source
  file gets a header.
- **No contributions accepted without CLA.** Even from Claude. Any code
  committed by automated tools on behalf of Richard counts as Richard's
  work.

## 6. Monetisation model — open core

SAE Books is **open core**. One codebase, five editions, runtime feature
flags decide what's enabled. Two licensing paths exist: **perpetual
licence** (buy once, own it — the Offline edition) and **subscription
licence** (monthly/annual, cloud or self-hosted — Business / Pro /
Enterprise). Community is always free.

Customer-facing licensing detail lives in `SPEC-LICENSING.md`; pricing
numbers, setup fees, and internal commercial policy live in
`SPEC-PRICING.md` (private repo). This charter fixes the shape; those
docs fix the numbers.

### 6.1 Community edition (free, AGPL-3.0)

The hero promise lives here. Community is not crippleware — it's a
complete, usable bookkeeping system for a single Australian small
business.

Community includes: full chart of accounts, journal entries, sales,
purchases, contacts, banking, reconciliation, GST/BAS **report
generation** (prep only, not e-lodgement), standard reports, full
export, Postgres or SQLite backend, immutable ledger audit mode,
single-company single-user runtime.

### 6.2 Offline edition (perpetual licence, USB-bound)

A once-off purchase for customers who want MYOB v19's ownership model —
pay once, own it, run on your own hardware, no phone-home. Target
audience: sole traders and small Pty Ltds who resent subscription
billing, and displaced MYOB AccountRight Classic v19 users (read-only
from 28 Feb 2026).

Offline includes everything in Community, **plus** all core
accounting-depth features that don't depend on our ongoing API costs:

- Multi-currency + FX revaluation (month-end adjusting/reversing)
- Inventory v1 (items with weighted-average cost)
- Projects + budgets
- Asset register v2 (linear + diminishing-value, partial disposal,
  CSV bulk import, tax-vs-book split)
- Multi-company runtime + intercompany journals
- Open Journal + Hybrid audit modes (in addition to Immutable)
- Granular permissions matrix (per §12.2)
- All themes (default, MYOB Classic, and any others we ship)
- BAS report generation for AU (prep only — e-lodgement is subscription-
  tier because ATO SBR has ongoing DSP certification costs)

Offline excludes only the paid-API integrations (bank feeds, ABR / LEI /
Companies House lookup, ATO SBR e-lodgement). Those are subscription
features because they cost us money per customer per month.

**USB-bound licensing model** — the licence file lives on a USB drive,
bound to the drive's immutable hardware identifier. The drive must be
present at startup and every 24h for the licence to validate. Activation
requires a one-time online handshake; everything after that runs
offline. See `SPEC-LICENSING.md` for the full protocol.

**Seat limit:** 1 admin user per Offline licence. Soft cap (warning
banner, full functionality retained) — the "WinRAR path."

**Company limit:** 1 company per Offline licence. Hard cap.

**Updates:** 12 months of updates included with every Offline purchase.
After that, the install runs forever on the version it has — users can
choose to keep it as-is, or buy an optional annual maintenance plan to
keep receiving updates. See §6.13.

### 6.3 Business edition (subscription, entry paid tier)

Target audience: typical Australian small businesses running on a single
legal entity, or small groups with two related entities, who want live
bank feeds and compliance automation without running their own licence
USB.

Business includes everything in Offline, **plus**:

- Bank feeds (SISS / ACSISS daily sync)
- ABR lookup (Australian Business Number enrichment on contacts)
- Stripe + Paperless integrations
- Email delivery of invoices (SAE-hosted SMTP relay)

**Seat limit:** 2 admin users + 3 employee users included; additional
seats available as paid add-ons. Hard cap — user creation blocked
when over limit with upgrade CTA.

**Company limit:** Up to 2 companies under one subscription. Hard cap.

### 6.4 Pro edition (subscription, mid tier)

Target audience: bookkeepers, accountants, and medium businesses running
consolidated groups of up to three entities.

Pro includes everything in Business, **plus**:

- LEI / GLEIF lookup
- UK Companies House lookup
- ATO SBR e-lodgement (BAS, and STP when offered)
- QuickBooks Online data import (migration tooling)
- Ad-hoc SQL query tool for power users
- Audit snapshot service (point-in-time financial snapshots)
- Automated scheduled backups

**Seat limit:** 5 admin users + 10 employee users included; additional
seats available as paid add-ons. Hard cap.

**Company limit:** Up to 3 companies under one subscription. Hard cap.

### 6.5 Enterprise edition (subscription, top tier)

Target audience: groups with more than three entities, professional
bookkeeping practices serving many clients, or any customer who needs
a support SLA.

Enterprise includes everything in Pro, **plus**:

- Unlimited companies
- Unlimited seats
- Per-company SISS credentials (separate bank-feed contracts per entity)
- Priority support with defined response-time SLA
- Signed releases + LTS (long-term support) branches
- Custom integrations (bespoke ERP / payroll / vertical connectors)
- Bespoke reporting and data-migration services
- Hosted SaaS option with same feature set

### 6.6 Two licensing paths

| Dimension | Perpetual (Offline) | Subscription (Business / Pro / Enterprise) |
|---|---|---|
| Payment | Once off (payment plan available) | Monthly or annual |
| Ownership | You own the licence forever | You rent access |
| Binding | USB hardware + online activation once | Ledger identifier (company legal entity) |
| Updates | 12 months included, optional annual maintenance thereafter | Included while subscription active |
| Paid APIs (feeds / lookups / lodgement) | Not included | Included |
| Seat / company caps | 1 / 1 | Per tier, see §6.3–6.5 |
| Upgrade path | → Subscription tier preserves all Offline features | → Higher subscription tier preserves all features (§6.9) |
| Loss of USB | One free replacement per 12 months; otherwise see SPEC-LICENSING | N/A |

### 6.7 User / seat model

Every user account carries a **seat class**:

- **Admin seat** — full administrative rights; maps to the `admin` role
  in the permissions matrix (§12.2).
- **Employee seat** — operational rights only; maps to any non-admin
  role (`accountant`, `bookkeeper`, `readonly`, `client`, or any
  customer-defined role that grants < full admin).

Seat caps are enforced separately per class. Downgrading an admin to an
employee frees an admin seat and consumes an employee seat, enforced at
role-change time. Upgrading consumes an admin seat.

### 6.8 Pricing anchors

The charter fixes the *shape*; commercial pricing lives in
`SPEC-PRICING.md`. For orientation only (not contractual):

- **Community:** $0
- **Offline:** single once-off charge, payment plan available over up to
  6 months with full access from day 1
- **Business / Pro:** monthly subscription with annual discount
  (roughly two months free for annual pre-pay)
- **Enterprise:** starts from a published monthly floor, with either an
  optional setup fee OR a 12-month lock-in at a higher monthly rate
  with no setup fee

All pricing under QBO / Xero / MYOB equivalents for comparable seat and
feature counts. Self-hosting is the reason the maths works: our marginal
cost per customer approaches zero.

### 6.9 Upgradeability guarantee

Every paid tier is a strict superset of the tier below. Moving up never
removes a feature a customer was already using. The canonical order is:

```
Community ⊂ Offline ⊂ Business ⊂ Pro ⊂ Enterprise
```

Offline is intentionally positioned as a full-featured *offline-capable*
edition (matches the Community→Offline→Business path for customers who
start Free, buy Offline, then later add cloud features). Upgrading from
Offline to Business means *adding* bank feeds, ABR, Stripe, Paperless,
plus cloud billing; nothing already in Offline disappears.

### 6.10 Revenue sources, in order of likelihood

1. **Subscription licences (Business / Pro / Enterprise).** Monthly or
   annual per-account fee.
2. **Offline perpetual licences.** Once-off purchase, optional annual
   maintenance plan.
3. **Hosted SaaS.** Same codebase, we run it. Enterprise features on.
   Target: users who like the philosophy but don't want to operate a
   server.
4. **Compliance APIs.** Paid hosted APIs (BAS SBR, STP, ABN / LEI / ABR
   lookup). Usable standalone or via subscription UI integration.
5. **Setup + migration services.** Optional data-import + training
   packs on paid tiers; custom migration for Enterprise.
6. **Plugin marketplace revenue share.** First-party premium plugins we
   publish; third-party plugin listings with revenue share (§14).
7. **Custom solutions.** Bespoke integrations, migrations, reporting.
8. **Partnerships.** Revenue share with certified bodies (LEI Register,
   ASIC data providers, bank-feed aggregators).

### 6.11 Ad slots (deferred, not enabled)

The UI ships with **named rendering slots** (e.g. dashboard sidebar,
empty-state panels) that are empty by default and config-driven. This
costs nothing to build in now and preserves the option to run
sponsored content in the hosted SaaS later without retrofitting the UI.
Self-hosted deployments never render ads unless the operator
explicitly enables a slot provider.

### 6.12 Acceptable Use Policy

SAE Books is sold under a commercial licensing agreement (EULA / ToS —
separate document). The licence is not available to, and may be
revoked from, any entity that:

1. Appears on the UN, Australian DFAT Consolidated List, US OFAC SDN
   list, EU CFSP sanctions list, or UK OFSI consolidated list.
2. Is materially involved in **armed conflict targeting civilians**,
   weapons manufacturing primarily targeting civilians, or
   state-sponsored violence.
3. Has its primary purpose documented as **human-rights violations**
   (modern slavery, child labour, persecution of identified groups
   on grounds of race, religion, gender, sexual orientation, or
   political opinion).
4. Is listed by recognised monitoring bodies (SPLC, ADL, Hope Not
   Hate, and Australian equivalents) as a hate organisation.

SAE Books reserves the right to terminate any commercial licence for
cause under the AUP. Data export rights persist for 30 days
post-termination — the hero promise does not die at the licensing
gate. Customers may appeal a termination to a human reviewer with a
14-day response SLA.

AGPL Community use is not gated by the AUP — AGPL rights cannot be
revoked under an acceptable-use clause. The AUP applies to **commercial
licences only** (Offline perpetual + subscription).

### 6.13 Updates policy (opt-in, loud)

The charter's position on updates is a direct consequence of the hero
promise ("your books, your control"):

- **No auto-update by default.** Container images don't pull new
  versions unless the operator configures it.
- **"Update available" banners are opt-in.** Default is silent.
- `/admin/updates` screen shows current version, latest version,
  changelog, and a single explicit button. Nothing runs without the
  click.
- **Your install keeps running, forever.** A customer who never
  updates still has a working accounting system on the version they
  bought. We never hold functioning code hostage behind a "your
  version is too old, upgrade or lose access" flow.
- Offline licences have a documented `updates_until` date (activation
  + 12 months by default, extended by maintenance renewals). Past that
  date, the app continues to run — only new-version pulls are gated.
- Subscription licences receive updates continuously while active. If
  the subscription lapses, the app continues to run on the last
  version that was current at lapse.

UI copy, verbatim, on every update-relevant screen: **"Your SAE Books
install will keep running exactly as-is, forever. Updates are
optional and are for you to choose when it suits you."**

### 6.14 Explicitly not doing

- **Paywalling core ledger, reporting, or BAS report generation.** Hero
  promise and AGPL spirit. The Community edition is real, not a demo.
- **Ads in self-hosted deployments by default.** Slots exist; they
  stay empty unless the operator opts in.
- **Selling user data.** Ever.
- **DRM / phone-home licence enforcement.** Licence keys validate
  locally against a public key bundled in the binary. Offline needs a
  one-time online activation handshake; no network call required to
  continue running after that. Subscription installs check in with the
  portal at a configurable interval (default weekly), but continue to
  run read-write during any outage of the portal up to the subscription
  grace period.
- **Forcing updates on customers to maintain functionality.** See §6.13.
- **Kill-switching customer books.** If a subscription lapses, the app
  goes to a read-only + export mode after the grace period; it never
  deletes or encrypts the customer's data.

If the product never earns revenue, Richard has still replaced a $40/mo
QBO subscription and owns his data outright. If it does earn revenue,
it's from Offline perpetual licences, subscription Business / Pro /
Enterprise gating, compliance plumbing, and the marketplace — not from
holding the ledger hostage.

### 6.15 Localisation packs — open or paid, by jurisdiction

SAE Books is multi-market by design (§1, §7.x). Adding a new country
is a localisation-pack exercise, not a fork. Localisation packs split
into two layers, with different licensing on each layer:

**Open layer (AGPL-3.0, community-maintainable):**

- Country chart of accounts (e.g. Odoo `l10n_au` / `l10n_uk` /
  `l10n_us` / `l10n_de` shape).
- Tax codes and rate tables (GST 10%, VAT 20%, US state sales-tax
  matrices, EU VAT rates, etc.).
- Report templates that don't require government certification —
  P&L, balance sheet, trial balance, ageing reports, custom layouts
  per jurisdiction.
- BAS-equivalent **report generation** (not lodgement) where the
  output is a paper-equivalent document the user lodges manually.
- Currency formatting, locale-aware date/number formatting, language
  translations.
- Bank-account format validators (BSB, IBAN, sort+account, ABA
  routing).

These ship as `saebooks-l10n-au`, `saebooks-l10n-uk`, etc. Each pack
is an AGPL repository accepting community contributions under the
SAE Books CLA. The community is free to maintain, extend, and
correct them — that's the whole point.

**Paid layer (commercial licence, SAE-maintained):**

- Certified e-lodgement engines: ATO SBR (BAS, STP), HMRC MTD (VAT,
  ITSA), IRS Modernized e-File, EU member-state e-invoicing schemes
  (PEPPOL UBL, FatturaPA, etc.). These have ongoing certification
  costs (DSP registration, security audits, API contract fees) that
  must be funded.
- Government-certified accounting engines where a jurisdiction
  requires audited certification of the software itself (rare, but
  exists in some EU jurisdictions and parts of LATAM).
- Tax-engine plug-ins covering complex calculation rules where errors
  carry direct customer liability (e.g. EU VAT MOSS, US sales-tax
  nexus calculation across 50 states).

Paid-layer packs are bundled into the Pro and Enterprise subscription
tiers (§6.4, §6.5). Customers running Community or Offline editions
can use the open layer fully, including report **generation**, and
lodge those reports manually with the relevant tax authority — the
paid layer is the *automation* of lodgement, not the ability to
comply.

**Why this split:**

- The open layer is where the community contributes — bookkeepers and
  accountants in every jurisdiction can fix errors in their country's
  CoA or tax tables without waiting for SAE Engineering. Mirrors
  Odoo's `l10n_*` model, which has produced 50+ community-maintained
  country packs.
- The paid layer is where the cost is. Government certifications
  expire, APIs change, security audits recur. Funding those through
  paid tiers keeps SAE Engineering able to maintain them; making them
  free would either burn out the maintainer or cause certifications
  to lapse.
- The split runs along the same axis as the rest of the open-core
  model: free where the marginal cost is zero, paid where it isn't.

**Boundary rule.** Code that decides *what to lodge* belongs in the
open layer. Code that *transmits the lodgement to a certified
endpoint* belongs in the paid layer. This rule resolves most edge
cases — a bookkeeper in any edition can produce a fully-correct BAS
on screen and lodge it manually; only the click-to-submit pipeline
costs money.

## 7. Architecture decisions

### 7.1 Tenancy

- **Schema is multi-tenant from day 1.** Every business-data table has
  a `company_id` FK. Migrations assume this. Retrofitting is a refactor
  nobody wants.
- **Company caps per edition** (hard-enforced at company-creation time):
  - Community: 1
  - Offline: 1
  - Business: up to 2
  - Pro: up to 3
  - Enterprise: unlimited
- **Multi-company / intercompany unlocks at Offline.** Multiple companies
  under one login, reciprocal journals between them — e.g. Sauer Pty Ltd
  and Saueesti Trust under one login. Offline gets this because it's a
  pure-code feature with no ongoing cost to us; Business+ raises the
  cap.
- **Self-compiling users can flip the flag.** AGPL reality. Accepted.
  The friction of rebuilding + self-supporting + forgoing signed
  releases, LTS, and support is the moat.
- **Future hosted SaaS** flips the same flag server-side to run
  multi-tenant across businesses. Same code, different deployment.

### 7.2 Audit / immutability model

Three modes exist in the codebase:

1. **Immutable ledger:** posted journal entries cannot be edited or
   deleted. Corrections are made via explicit reversal entries. Full
   audit trail of who posted what when.
2. **Open journal:** posted entries can be edited, but every edit is
   logged with actor, timestamp, and before/after snapshot. More
   convenient for bookkeepers who know what they're doing; still
   forensically recoverable.
3. **Hybrid:** entries editable within the current period until period
   close; immutable after. Matches how MYOB/Xero/QBO actually behave
   in practice.

**Edition gating:**

- **Community edition:** Immutable ledger only. Not configurable.
  This is the safest default for self-hosters and removes a class
  of foot-guns for users who aren't trained bookkeepers.
- **Enterprise edition:** All three modes available, per-company,
  changeable at runtime (with the change itself audited).

The setting lives in `settings.audit_mode` and is visible on every
screen that writes data. A regulator auditing a Hybrid-mode business
sees the period-close events and can reason about immutability windows.

### 7.3 Data integrity non-negotiables

For v1 to be "real," these are bright lines enforced at the DB, not just
app code:

- Every financial mutation is a single DB transaction. Partial writes
  are impossible.
- Journal entries sum to zero (debits = credits) — enforced by deferred
  constraint or trigger.
- Decimal money type throughout (Postgres `NUMERIC(18,4)` internal,
  presented at the configured currency's decimal places). **Never
  float.**
- Tax rounding policy per the spec (DOWN on sales, UP on purchases,
  per-line default, per-document toggle, configurable per tax code).
- Full audit log table: actor, timestamp, IP, request ID, table, row,
  before-JSON, after-JSON. Append-only.
- Reconciliation locks freeze their journal lines against edits even
  in open-journal mode.
- Period locks freeze entire periods against new or edited entries.
- Property-based tests (Hypothesis) for all money arithmetic:
  tax rounding, FX conversion, multi-line invoice totals, multi-entry
  reconciliation.

### 7.4 Local-first, DB-portable

- Postgres is the default and primary supported DB. We support all
  Postgres versions currently in community support.
- SQLite supported as a "single-file, single-user, no-server" option
  for smallest deployments (sole traders running on a laptop).
  Migrations must work on both.
- Full DB dump + attached PDFs as a standard export format. Importable
  into a fresh instance with one command.
- No silent phone-home. No telemetry enabled by default. If we ever add
  opt-in telemetry, it's opt-in, documented, and users can run a
  `saebooks audit` command that lists every network endpoint the app
  ever touches.

### 7.5 Stack

Same shape as the lei-agent project, which proves it works for this
kind of app:

- **Backend:** FastAPI + async SQLAlchemy + Alembic.
- **Frontend:** Server-rendered HTMX + Alpine.js. Hand-rolled CSS. No
  React, no bundler in the critical path. The desktop-density UI is
  part of the product.
- **Database:** Postgres 15+ (or SQLite 3.38+ for the tiny deployments).
- **Auth:** Email/password + magic link, plus OAuth (GitHub, Google,
  Microsoft) for our hosted deployments. Self-hosted users can use
  any OIDC provider or local username/password.
- **PDF rendering:** reuse the Chromium renderer from lei-agent.
- **Background jobs:** APScheduler or RQ, added only when a feature
  needs it. No Celery unless we're forced.
- **Tests:** pytest + Hypothesis. Target 90%+ coverage on money
  arithmetic paths, 80%+ overall.
- **Plugin / extension architecture:** a first-class plugin system
  (see §14) lets the core stay small while letting third parties
  extend sales, purchases, reporting, payments, imports. Plugins are
  Python packages that register extension points declared by the
  core — not iframes, not embedded browser views.
- **Named rendering slots:** the UI renders through explicit slots
  (e.g. `dashboard.sidebar`, `invoice.footer`, `account.empty_state`).
  Slots are empty by default and config-driven. Plugins and the
  future ad provider both use the same mechanism; the core never
  knows the difference.
- **Edition flag:** a runtime config key `edition = community |
  enterprise` gates §6.2 features. Validated at startup from a
  signed licence key (Enterprise only); Community needs no key.

## 8. Governance

- **Benevolent dictator model** initially. Richard Sauer decides.
- **Single maintainer** for at least the first 12 months.
- **External contributions welcome from day 1 of going public**, gated
  on CLA.
- **Public roadmap** once public, maintained in `docs/ROADMAP.md` and
  referenced from every release.
- **Semver discipline.** Breaking changes (schema migrations that lose
  data, API changes) require a major version bump and migration notes.
  Self-hosted users upgrading in-place must never lose data — we run
  our own instance through every migration before releasing.

## 9. Documentation policy

- Every feature ships with user-facing docs. Undocumented features
  don't exist, and PRs without docs don't merge.
- User guide lives in `docs/` as Markdown, rendered as a static site
  at `books.saee.com.au/docs/`.
- API reference auto-generated from FastAPI's OpenAPI output.
- Admin runbook separate from user docs — covers install, backup,
  restore, upgrade, migrate, audit.

## 10. Privacy and launch timing

- **Repository is private** (Gitea, internal) until we explicitly choose
  to publish.
- **Publish criteria:** Richard uses SAE Books as his primary accounting
  system for at least one full BAS quarter without falling back to QBO.
  If we can't trust it, we can't publish it.
- **Publish channels when ready:** GitHub (`sae-engineering/saebooks`),
  announcement on SAE Engineering site, Hacker News, Reddit
  r/AusFinance + r/selfhosted, appropriate AU accounting / small-
  business forums.
- **Marketing is not a v1 concern** but is deliberately not being
  ignored — the charter exists partly so marketing doesn't look
  confused when it arrives.

## 11. Existential risk register

Writing these down so we can't pretend they weren't known:

| Risk | Mitigation |
|---|---|
| Time commitment exceeds Richard's available runway. | Building for himself first — even a truncated v1 is useful the day it replaces QBO. |
| AU tax rules change and we fall behind. | Core charter-locks us out of the compliance-certification trap (no payroll/STP in v1). BAS field mapping is data-driven (Odoo's CoA tags), so tag changes don't require code changes. |
| Odoo or another incumbent ships a comparable open-source AU-compliant bookkeeping app. | Accept the outcome. Nobody has in 15 years — not a high-probability risk. |
| Data corruption bug in pre-v1 destroys Richard's live books. | Parallel-run with QBO for one full quarter before cutover. Automated DB backup every hour. Point-in-time recovery tested monthly. |
| CLA wording inadequate for future commercial licensing. | Use a known-good template (Apache ICLA-derived). Lawyer review before first external contribution. |

## 12. Edition matrix

Unambiguous split so nobody has to guess what's in which tier. Paid
tiers are **strict supersets** of the tiers below (§6.9). Community and
Offline are both self-hosted-only; Business / Pro / Enterprise ship as
either self-hosted or hosted SaaS at the customer's choice.

### 12.1 Feature matrix

| Capability | Community | Offline | Business | Pro | Enterprise |
|---|---|---|---|---|---|
| Chart of accounts, journal, GL | ✅ | ✅ | ✅ | ✅ | ✅ |
| Sales, purchases, contacts | ✅ | ✅ | ✅ | ✅ | ✅ |
| Bank reconcile (CSV / OFX import) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Bank rules (auto-categorisation) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Fixed assets v1 (linear, full disposal) | ✅ | ✅ | ✅ | ✅ | ✅ |
| GST / BAS report **generation** | ✅ | ✅ | ✅ | ✅ | ✅ |
| Full export (CSV, JSON, OFX, QIF, DB dump) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Postgres + SQLite | ✅ | ✅ | ✅ | ✅ | ✅ |
| Immutable audit mode | ✅ | ✅ | ✅ | ✅ | ✅ |
| Recurring invoices, credit notes, payments | ✅ | ✅ | ✅ | ✅ | ✅ |
| Pay run + ABA file generation | ✅ | ✅ | ✅ | ✅ | ✅ |
| Multi-currency + FX revaluation | ❌ | ✅ | ✅ | ✅ | ✅ |
| Inventory v1 (items, WAC) | ❌ | ✅ | ✅ | ✅ | ✅ |
| Projects + budgets | ❌ | ✅ | ✅ | ✅ | ✅ |
| Fixed assets v2 (DV dep, partial disposal, CSV, tax/book) | ❌ | ✅ | ✅ | ✅ | ✅ |
| Multi-company / intercompany runtime | ❌ | ✅ (1) | ✅ (2) | ✅ (3) | ✅ (∞) |
| Open Journal + Hybrid audit modes | ❌ | ✅ | ✅ | ✅ | ✅ |
| Granular permissions matrix | ❌ | ✅ | ✅ | ✅ | ✅ |
| All themes (MYOB Classic, SS, TT, etc.) | ❌ | ✅ | ✅ | ✅ | ✅ |
| Bank feeds (SISS daily sync) | ❌ | ❌ | ✅ | ✅ | ✅ |
| ABR lookup | ❌ | ❌ | ✅ | ✅ | ✅ |
| Stripe + Paperless integrations | ❌ | BYO keys | ✅ | ✅ | ✅ |
| SAE-hosted SMTP for invoice delivery | ❌ | ❌ | ✅ | ✅ | ✅ |
| LEI / GLEIF lookup | ❌ | ❌ | ❌ | ✅ | ✅ |
| UK Companies House lookup | ❌ | ❌ | ❌ | ✅ | ✅ |
| ATO SBR e-lodgement (BAS, STP) | ❌ | ❌ | ❌ | ✅ | ✅ |
| QuickBooks Online import | ❌ | ❌ | ❌ | ✅ | ✅ |
| SQL query tool | ❌ | ❌ | ❌ | ✅ | ✅ |
| Audit snapshot service | ❌ | ❌ | ❌ | ✅ | ✅ |
| Automated scheduled backups | ❌ | ❌ | ❌ | ✅ | ✅ |
| Per-company SISS credentials | ❌ | ❌ | ❌ | ❌ | ✅ |
| Priority support + SLA | ❌ | ❌ | ❌ | ❌ | ✅ |
| Signed releases + LTS branches | ❌ | ❌ | ❌ | ❌ | ✅ |
| Custom integrations + bespoke reporting | ❌ | ❌ | ❌ | ❌ | ✅ |
| Marketplace plugin distribution (first-party) | ❌ | ✅ | ✅ | ✅ | ✅ |
| Source available | ✅ (AGPL) | ✅ (AGPL + commercial) | ✅ (AGPL + commercial) | ✅ (AGPL + commercial) | ✅ (AGPL + commercial) |
| Self-compile to flip feature flags | ✅ allowed | ✅ allowed | ✅ allowed | ✅ allowed | ✅ allowed |

Flipping a flag in a self-compiled build is never a licence violation.
Running an unflagged commercial-licensed binary in production *is*.

### 12.2 Seat matrix

| Edition | Admin seats | Employee seats | Seat cap enforcement | Paid add-on seats |
|---|---|---|---|---|
| Community | 1 | 0 | Hard | — |
| Offline | 1 | 0 | **Soft** (warning banner) | — |
| Business | 2 | 3 | Hard | Available |
| Pro | 5 | 10 | Hard | Available |
| Enterprise | ∞ | ∞ | N/A | N/A |

"Hard" = user creation is blocked at the limit with an upgrade CTA.
"Soft" = functionality retained; banner only. Offline is soft because
it's an owned perpetual licence on a single physical device — the USB
binding itself is the real limit (see §6.2 and `SPEC-LICENSING.md`).

Admin vs employee mapping to permission roles is fixed in §6.7.

### 12.3 Company matrix

| Edition | Company cap | Enforcement |
|---|---|---|
| Community | 1 | Hard |
| Offline | 1 | Hard |
| Business | 2 | Hard |
| Pro | 3 | Hard |
| Enterprise | ∞ | N/A |

## 13. Plugin integration guidelines

The product must feel like one application, not a skinned aggregator
of third-party bolt-ons. MYOB v19's "Feature Pass" bundles and QBO's
App Store both fail this bar — plugins live in iframes, carry
inconsistent UX, re-authenticate against foreign systems, and leak
their own terminology into the user's mental model. SAE Books plugins
must not feel like that. The guidelines below are non-optional for
listing in the first-party marketplace and strongly recommended for
all plugins.

### 13.1 What a plugin is

- A Python package that declares extension points (hooks, commands,
  UI slots, domain objects) against the SAE Books core plugin API.
- Installed into the same Python environment as the core. Runs
  in-process, uses the same database session, the same auth, the
  same request/response cycle.
- Distributed via the SAE Books plugin index (first-party) or
  pip/git (third-party).

### 13.2 What a plugin is not

- **Not an iframe.** No embedding third-party web apps inside the
  core UI via `<iframe>`, `<webview>`, or equivalent.
- **Not a separate login.** Plugins use the core's authenticated
  session. No "connect your Xyz account" flows that pop a second
  auth surface *inside* the app. (External OAuth to third-party
  services is fine — that's the plugin acting on behalf of the
  user, not the user authenticating twice.)
- **Not a separate visual language.** No plugin-specific fonts,
  colour palettes, button shapes, or layout grids. The core ships
  a design system; plugins consume it.
- **Not a data island.** Plugins must store data in the same
  Postgres database, in tables they declare via core-provided
  migration helpers. No side databases, no plugin-owned SaaS
  backends holding the user's financial data.

### 13.3 Required integration surfaces

A compliant plugin uses these and only these:

1. **Hooks.** Named extension points the core emits (e.g.
   `journal.before_post`, `invoice.after_send`, `report.bas.render`).
   Plugins subscribe; the core calls them in-process.
2. **Named UI slots.** The core renders through `{% slot "name" %}`
   in HTMX templates. Plugins register slot contributions that
   return HTML using the core's component library. Slot contributions
   must be CSP-compliant and must not load external scripts or
   stylesheets.
3. **Domain objects.** Plugins can declare new doctypes (e.g.
   "Lease", "Fixed Asset Register") via the core's ORM layer.
   These participate in the core audit log automatically.
4. **CLI commands.** Plugins can register `saebooks <plugin>
   <command>` subcommands for admin/maintenance tasks.
5. **API endpoints.** Plugins can mount FastAPI routers under
   `/api/plugin/<name>/`. Same auth middleware, same rate limits,
   same OpenAPI generation as the core.

### 13.4 Plugin manifest

Every plugin ships `saebooks.toml` declaring:

- Name, version, author, licence
- AGPL-compatibility statement (required for Community-marketplace
  listing; Enterprise-marketplace plugins may be any licence)
- Minimum SAE Books core version
- Declared hooks, slots, doctypes, routes
- Required core permissions
- Third-party network endpoints the plugin will contact (shown to
  the user at install time, per §7.4)

### 13.5 First-party marketplace review

Plugins listed in the first-party marketplace go through review:

- Manifest accurate and complete
- No iframe / webview / external-script usage
- Uses the core design system components, not custom-rolled UI
- Declared third-party endpoints match what the code actually
  calls (we run it behind a recording proxy during review)
- Security review proportional to permissions requested
- Licence compatible with listing tier

Third-party plugins distributed outside the marketplace are welcome
but carry a "unverified plugin" warning at install time and cannot
use the first-party marketplace namespace.

### 13.6 Why this matters

Every bookkeeping product that has tried to be an ecosystem without
enforcing integration standards has ended up feeling like a portal
of other people's websites. The core value proposition — *your
books, your database, your control* — dies the moment a plugin
slots an iframe over half the screen and asks the user to log into
a third-party service to see their own data. This is a hard line.

## 14. Reference material

Extracted during project kickoff, retained as source material:

- `/home/sauer/projects/infra-blueprint/teardowns/myob-v19/SPEC.md`
  — the detailed technical spec (subordinate to this charter).
- `/home/sauer/projects/infra-blueprint/teardowns/myob-v19/reference/`:
  - `odbc_user_guide.pdf` — MYOB ODBC Direct v10 user guide (494 pp.)
    Authoritative MYOB data model. 48+ business tables documented.
  - `Clearwtr.MYO` — MYOB Clearwater sample company file.
  - `odoo-l10n-au/` — Odoo Community AU chart of accounts + tax codes
    + BAS tags. This is our v1 seed.

## 15. What's next

The immediate technical next step is Phase 1 of `SPEC.md`:

1. Repository scaffold: FastAPI + Alembic + Postgres + Docker Compose.
2. Settings table and settings admin screen first — every downstream
   feature reads config from it.
3. Companies table (multi-tenant foundation).
4. Load Odoo AU CoA into seed SQL.
5. Accounts table, accounts list screen, journal entry posting.

No code lives outside the repo. No shortcuts around the audit model.
Every commit signed, every PR (once public) CLA-checked.

---

*Charter v1.0 — 2026-04-15*
*Charter v1.1 — 2026-04-22 — Amended §6 (five-edition model + Offline perpetual + USB-bound licensing + AUP + updates policy), §7.1 (company caps per edition), §12 (five-column feature matrix + seat matrix + company matrix). Companion docs: `SPEC-LICENSING.md` (public), `SPEC-PRICING.md` (private), `EULA.md` draft (private, pending lawyer review).*
*Charter v1.2 — 2026-04-26 — Amended §1 (multi-market readiness in scope at schema/API layer; certified non-AU lodgement deferred), §5 (added saebooks-web AGPL coverage + trademark + concrete CLA file references), and added §6.15 (localisation pack open/paid split — open CoA + tax codes + report generation, paid certified e-lodgement). Companion docs: `LICENSING.md` (top-level summary), `TRADEMARK.md` (brand policy), `CLA.md` (Apache ICLA derivative). `saebooks-web` LICENSE file added (AGPL-3.0).*
