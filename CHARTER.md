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

## 6. Monetisation model — open core

SAE Books is **open core**. Two editions, one codebase, runtime feature
flags decide what's enabled.

### 6.1 Community edition (free, AGPL-3.0)

The hero promise lives here. Community is not crippleware — it's a
complete, usable bookkeeping system for a single Australian small
business.

Community includes: full chart of accounts, journal entries, sales,
purchases, contacts, banking, reconciliation, GST/BAS reporting,
standard reports, full export, SQLite or Postgres backend, immutable
ledger audit mode, single-company runtime.

### 6.2 Enterprise edition (paid, commercial licence)

Same codebase. The features below are compiled in but gated behind a
runtime licence-key flag. Anyone who builds from source can flip the
flag — that's AGPL. Practically, the friction of rebuilding and
self-supporting is the moat, not DRM.

Enterprise adds:

- **Multi-company / intercompany.** Multiple companies under one
  login, reciprocal journals between them, consolidated reporting.
  The schema is always multi-tenant; Enterprise unlocks the UI and
  runtime to use it.
- **Ledger audit mode choice.** Community is immutable-only.
  Enterprise unlocks Open Journal and Hybrid modes (per §7.2).
- **Compliance API access from the app.** Built-in BAS submission,
  STP (when offered), ABN/LEI/ABR/Companies-House lookup, e-invoicing.
  Community users can call those APIs directly (they're separate paid
  services); Enterprise bundles them into the UI.
- **Priority support + signed releases + LTS branches.**

### 6.3 Revenue sources, in order of likelihood

1. **Enterprise licence.** Annual per-company fee. Priced well below
   QBO/Xero for equivalent functionality.
2. **Hosted SaaS.** Same codebase, we run it. Enterprise features on.
   Target: users who like the philosophy but don't want to operate a
   server.
3. **Compliance APIs.** Paid hosted APIs (BAS, STP, ABN/LEI/ABR).
   Usable standalone or via Enterprise UI integration.
4. **Plugin marketplace revenue share.** First-party premium plugins
   we publish; 3rd-party plugin listings with revenue share (see §14).
5. **Custom solutions.** Bespoke integrations, migrations, reporting.
6. **Partnerships.** Revenue share with certified bodies (LEI
   Register, ASIC data providers, bank-feed aggregators).

### 6.4 Ad slots (deferred, not enabled)

The UI ships with **named rendering slots** (e.g. dashboard sidebar,
empty-state panels) that are empty by default and config-driven. This
costs nothing to build in now and preserves the option to run
sponsored content in the hosted SaaS later without retrofitting the UI.
Self-hosted deployments never render ads unless the operator
explicitly enables a slot provider.

### 6.5 Explicitly not doing

- **Paywalling core ledger, reporting, or BAS.** Hero promise and
  AGPL spirit. The Community edition is real, not a demo.
- **Ads in self-hosted deployments by default.** Slots exist; they
  stay empty unless the operator opts in.
- **Selling user data.** Ever.
- **DRM / phone-home licence enforcement.** Enterprise licence keys
  validate locally against a public key. No network call required to
  run the product.

If the product never earns revenue, Richard has still replaced a $40/mo
QBO subscription and owns his data outright. If it does earn revenue,
it's from Enterprise gating, compliance plumbing, and the marketplace
— not from holding the ledger hostage.

## 7. Architecture decisions

### 7.1 Tenancy

- **Schema is multi-tenant from day 1.** Every business-data table has
  a `company_id` FK. Migrations assume this. Retrofitting is a refactor
  nobody wants.
- **Community runtime is single-tenant.** A runtime flag enforces
  exactly one active `company_id` per deployment. The schema supports
  more; the Community binary refuses to use them.
- **Enterprise runtime unlocks multi-company / intercompany.**
  Multiple companies under one login, reciprocal journals between
  them — e.g. Sauer Pty Ltd and Saueesti Trust under one login. That's
  the genuine gap in the market and the single biggest Enterprise
  hook.
- **Self-compiling users can flip the flag.** AGPL reality. Accepted.
  The friction of rebuilding + self-supporting is the moat.
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
- **Auth:** Authentik OIDC / forward-auth via Caddy for our hosted
  deployments. Self-hosted users can use any OIDC provider or local
  username/password.
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

Unambiguous split so nobody has to guess what's in which tier.

| Capability | Community (AGPL, free) | Enterprise (paid) |
|---|---|---|
| Chart of accounts, journal, GL | ✅ | ✅ |
| Sales, purchases, contacts | ✅ | ✅ |
| Bank reconcile | ✅ | ✅ |
| GST / BAS report generation | ✅ | ✅ |
| Full export (CSV, JSON, OFX, QIF, DB dump) | ✅ | ✅ |
| Postgres + SQLite | ✅ | ✅ |
| Single-company runtime | ✅ | ✅ |
| Immutable audit mode | ✅ | ✅ |
| Multi-company / intercompany runtime | ❌ | ✅ |
| Open Journal / Hybrid audit modes | ❌ | ✅ |
| Built-in BAS/STP/ABN API UI | ❌ | ✅ |
| Priority support + signed LTS releases | ❌ | ✅ |
| Marketplace plugin distribution (first-party) | ❌ | ✅ |
| Source available | ✅ (AGPL) | ✅ (AGPL; Enterprise features under commercial licence) |
| Self-compile to flip feature flags | ✅ allowed | ✅ allowed |

Flipping a flag in a self-compiled build is never a licence violation.
Running an unflagged commercial-licensed binary in production is.

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
