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
  pit), multi-currency stays configurable but is a v1.1 priority, live
  bank feeds are v1.1, inventory belongs in a sibling project.
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

- **Core code:** AGPLv3. Copyleft strong enough that commercial forks
  can't re-host as a proprietary SaaS without contributing back or
  buying a commercial exemption from us.
- **CLA (Contributor License Agreement):** required from every external
  contributor starting from the first pull request. Gives Richard Sauer
  (or assignee) the right to re-license contributions under a commercial
  licence in addition to AGPL. Standard wording — modelled on the Apache
  ICLA. Automated via a GitHub CLA bot once the repo is public.
- **Copyright:** © 2026 Richard Sauer / SAE Engineering. Every source
  file gets a header.
- **No contributions accepted without CLA.** Even from Claude. Any code
  committed by automated tools on behalf of Richard counts as Richard's
  work.

## 6. Monetisation model

The core product is free, open-source, forever. Revenue sources, in
order of likelihood:

1. **Compliance APIs.** Paid hosted API for BAS submission, STP (when
   relevant), ABN/LEI/ABR/Companies-House lookup. The lei-agent project
   already proves we can build these. Small businesses pay a flat
   annual fee to submit through us rather than the ATO portal UI.
2. **Hosted SaaS.** Same codebase, we run it. Target: users who like
   the philosophy but don't want to operate a server. Priced clearly
   against QBO/Xero, with the honest pitch: "Exactly the same software
   you could run yourself."
3. **Custom solutions.** Bespoke integrations, migrations, reporting,
   industry-specific customisations. Project-based.
4. **Partnerships.** Revenue share with certified bodies (e.g. LEI
   Register for LEI issuance, ASIC data providers, bank-feed
   aggregators). We front their service as a one-click feature; they
   give us margin.

**Explicitly not doing:**

- Paywalling core ledger, reporting, or BAS. That would break the hero
  promise and the AGPL spirit.
- Ads. Ever.
- Selling user data. Ever.

If the product never earns revenue, Richard has still replaced a $40/mo
QBO subscription and owns his data outright. If it does earn revenue,
it's from plumbing value-added services on top, not from holding the
ledger hostage.

## 7. Architecture decisions

### 7.1 Tenancy

- **Schema is multi-tenant from day 1.** Every business-data table has
  a `company_id` FK. Migrations assume this. Retrofitting is a refactor
  nobody wants.
- **Deployment is single-tenant by default.** One Docker container per
  business. Self-hosted users get privacy and simplicity. The multi-
  tenant capability is used primarily for **intercompany support within
  a single logged-in user** — e.g. Sauer Pty Ltd and Saueesti Trust
  under one login, with reciprocal journals between them. That's a
  genuine gap in the market.
- **Future hosted SaaS** flips one flag to run multi-tenant across
  businesses. Same code, different deployment config.

### 7.2 Audit / immutability model

User-configurable per-company setting with three modes:

1. **Immutable ledger** (default for paranoid users): posted journal
   entries cannot be edited or deleted. Corrections are made via
   explicit reversal entries. Full audit trail of who posted what when.
2. **Open journal:** posted entries can be edited, but every edit is
   logged with actor, timestamp, and before/after snapshot. More
   convenient for bookkeepers who know what they're doing; still
   forensically recoverable.
3. **Hybrid** (recommended default): entries editable within the
   current period until period close; immutable after. Matches how
   MYOB/Xero/QBO actually behave in practice.

The setting is per-company and changeable (with the change itself
audited). A regulator auditing a Hybrid-mode business sees the
period-close events and can reason about immutability windows. This
decision goes in `settings.audit_mode` and is visible on every screen
that writes data.

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
- **Auth:** Authentik OIDC / forward-auth via Caddy for our hosted
  deployments. Self-hosted users can use any OIDC provider or local
  username/password.
- **PDF rendering:** reuse the Chromium renderer from lei-agent.
- **Background jobs:** APScheduler or RQ, added only when a feature
  needs it. No Celery unless we're forced.
- **Tests:** pytest + Hypothesis. Target 90%+ coverage on money
  arithmetic paths, 80%+ overall.

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

## 12. Reference material

Extracted during project kickoff, retained as source material:

- `/home/sauer/projects/infra-blueprint/teardowns/myob-v19/SPEC.md`
  — the detailed technical spec (subordinate to this charter).
- `/home/sauer/projects/infra-blueprint/teardowns/myob-v19/reference/`:
  - `odbc_user_guide.pdf` — MYOB ODBC Direct v10 user guide (494 pp.)
    Authoritative MYOB data model. 48+ business tables documented.
  - `Clearwtr.MYO` — MYOB Clearwater sample company file.
  - `odoo-l10n-au/` — Odoo Community AU chart of accounts + tax codes
    + BAS tags. This is our v1 seed.

## 13. What's next

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
