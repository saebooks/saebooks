# SAE Books

> *Your books. Your database. Your control.*

**Self-hosted, API-first double-entry accounting for Australian small-to-medium
businesses — with a multi-market-ready core.**

SAE Books keeps your financial data in a database *you* control — SQLite in a
single folder for the one-device Community install, Postgres for server
installs. Your laptop, your NAS, a VPS you rent: same code everywhere.
The web UI is a thin client over a public REST API, so anything you can do in a
browser your scripts can do too — same endpoints, same auth, same OpenAPI schema.

**▶ [Download](https://saebooks.com.au/download)** · [Live demo](https://cashbook-demo.saebooks.com.au) · [Developer docs](https://dev.saebooks.com.au)

## Status

| | |
|---|---|
| Phase | **Public BETA** |
| Current releases | One-click server **v0.6** · Docker images **v0.6.1** · Desktop clients **v0.4** |
| Licence | AGPLv3 + commercial dual licence (see [LICENSE](./LICENSE), [LICENSING.md](./LICENSING.md)) |
| Contributions | Welcome — a CLA is required from the first pull request (see [CLA.md](./CLA.md)) |

**Honest beta notes:**

- The Community edition ships the **full core bookkeeping engine** — nothing in
  day-to-day double-entry is feature-gated.
- **No electronic tax lodgement yet.** The engine prepares BAS/GST worksheets
  and returns, but certified transmission to a tax authority (e.g. ATO SBR) is
  a commercial service still in accreditation — the open build stubs it out
  rather than transmitting.
- **Binaries are unsigned.** Windows SmartScreen and macOS Gatekeeper will warn
  on first run; verify your download against the published `SHA256SUMS`.
- **Estonian and Russian translations are machine-translated** and not yet
  human-reviewed.
- Pre-1.0: keep your own backups.

The [Charter](./CHARTER.md) is the senior document in this repository — it
captures the strategic decisions that shape SAE Books, and every engineering
doc is subordinate to it.

## Install

### 1. One-click server (recommended)

One file, no Docker, no dependencies. Download from the
[v0.6 release](https://github.com/saebooks/saebooks/releases/tag/v0.6):

| Platform | File | Steps |
|---|---|---|
| Windows | `SAEBooks-windows-x64.exe` | Double-click. SmartScreen will show "Windows protected your PC" — click **More info → Run anyway** (the binary is unsigned; expected). First run unpacks for about a minute, once only. |
| Linux | `SAEBooks-linux-x86_64` | `chmod +x SAEBooks-linux-x86_64 && ./SAEBooks-linux-x86_64` |
| macOS | *build pending* | A macOS one-click build is coming; use the Docker path below in the meantime. |

The launcher starts the engine and web UI on localhost and opens your browser
at **http://127.0.0.1:18960** (web UI; API on `127.0.0.1:18961`). Sign in with
the starter books — `you@example.com` / `change-me-now` — and change the
password. Your books live in one folder on your machine (the console window
shows where); back that folder up and you've backed up your books.

Full walk-through, file locations, and troubleshooting:
[installers/oneclick/INSTALL.md](./installers/oneclick/INSTALL.md).
Verify downloads against the release's `SHA256SUMS`.

### 2. Docker (Community bundle)

One command, three containers — the API engine, the web UI, and a XeLaTeX
render service (`latex-api`, new in v0.6.1: invoice/statement PDFs and
email attachments). SQLite-backed, no Postgres or migrations to run:

```bash
curl -fsSLO https://raw.githubusercontent.com/saebooks/saebooks/main/docker-compose.community.yml
docker compose -f docker-compose.community.yml up -d
```

Open **http://127.0.0.1:18960** and sign in with the starter books
(`you@example.com` / `change-me-now`). The API is on `127.0.0.1:18961`.
Images are pulled from Docker Hub — `saebooks/saebooks-community-api`,
`saebooks/saebooks-community-web`, `saebooks/saebooks-community-latex`
(tags `v0.6.1` + `latest`, multi-arch `linux/amd64` + `linux/arm64`, so they
run natively on Intel/AMD and on Apple Silicon / Raspberry Pi class ARM).

Before exposing the stack beyond `127.0.0.1`, read the SECURITY NOTE in the
compose file: set your own secrets and change the starter password.

### 3. From source

For development, or a Postgres-backed server install:
[`deploy/community/`](./deploy/community/) is the reference Postgres
topology (engine + web + latex-api + Postgres via `docker compose`), and the
[developer quickstart](https://dev.saebooks.com.au/getting-started/quickstart/)
covers cloning the engine and `saebooks-web` repos and building images
yourself.

### Desktop clients (optional)

Native Qt desktop clients (Windows MSI, Linux AppImage) live in
[`saebooks-desktop`](https://github.com/saebooks/saebooks-desktop) —
**v0.4** auto-detects and pairs with a locally running one-click server.
macOS is at v0.3.0 (DMG) until the next build round. The Estonian brand of the
same client ships as **tasur** ([tasur.ee](https://tasur.ee); Estonian and
Russian UI is machine-translated pending human review).

## Why SAE Books

Three things the major cloud vendors can't say:

- **API-first, not GUI-first.** Every screen is a thin client over the same
  OpenAPI-documented endpoints your scripts call — bearer tokens, webhooks,
  idempotency keys. No screen-scraping, no "export to CSV and pray".
- **Service-oriented by default.** The ledger does one thing well: keeps the
  books. Document vault, bank-feed relay, lodgement, and payroll are separate
  services that talk to it over the API. Swap any of them; the books stay yours.
- **Your data, permanently yours.** AGPLv3 means the source is yours. Full
  export to CSV, JSON, OFX, or a complete DB dump — cancel any time and your
  data walks with you. No vendor can revoke access to your own ledger.

The core app works fully offline. Internet-dependent features (bank feeds, FX
rates, ABN lookup, BAS e-lodgement) are additive, never required.

## What's in the box

A platform first, a BAS app second. A multi-company ledger with audit trail,
period locks and FX sits underneath — the Australian tax bits sit on top of
that, not the other way round.

- **Core ledger** — chart of accounts, general ledger, sales, purchases,
  contacts, banking, GST/BAS worksheets.
- **Full record set** — invoices (with email send), quotes, bills, expenses,
  payments and receipts, transfers, customer and supplier credit notes,
  journals.
- **Bank import & reconciliation** — CSV/OFX statement import with a
  preview/apply flow, then match/unmatch reconciliation in the UI.
- **Reports & exports** — P&L, balance sheet, trial balance, aged
  receivables/payables and more; every report exports to CSV/XLSX, with PDF
  export for P&L, balance sheet, and trial balance.
- **Period locks, reclassifications & audit ledger** — close a period, lock
  it, reclassify with a trail; every change to a posted journal is
  tamper-evident. Company-driven financial-year period picker on reports.
- **Public REST API + API tokens** — the same endpoints the UI uses,
  documented via OpenAPI; issue bearer tokens from the settings area.
- **Multi-company** *(licensed)* — multiple entities under one login,
  reciprocal journals, consolidated reports, row-level tenant isolation.
- **Immutable invoice snapshots** — the rendered PDF is stored byte-identical;
  reprintable forever, built for dispute defence.
- **FX revaluation** — multi-currency posting with end-of-period revaluation.
- **Purchase orders, fixed assets, ABR lookup, time tracking.**
- **Jurisdiction-neutral core with country modules** — AU, UK, NZ, EE, LT, LV
  bolt-ons for local identifiers, tax codes, and reference charts.

Tier-gated / in progress (honest about maturity):

- **STP payroll** — PAYG formula engine, super, leave accrual, STP Phase 2
  payload assembly. *In beta — verify ATO tax-table coefficients before
  production use.*
- **BAS e-lodgement** — worksheet generation is built; direct ATO/SBR
  submission ships with the DSP-accredited Enterprise tier (ATO onboarding in
  progress).
- **Bank feeds** — SISS/ACSISS-backed daily feeds via our relay service, rolled
  out to tenants under the data-aggregator agreement.

## Architecture

SAE Books is a small constellation of services rather than one monolith:

| Component | What it is |
|---|---|
| [`saebooks`](.) | The ledger engine — FastAPI, SQLite or Postgres. A **pure API service** (REST + MCP + gRPC/Connect), no embedded UI. AGPLv3 + commercial. |
| [`saebooks-web`](https://github.com/saebooks/saebooks-web) | Server-rendered frontend — FastAPI + Jinja2 + HTMX, no build step. A thin client over the API, and the only bookkeeper UI. AGPLv3 + commercial. |
| [`latex-api`](./latex-api/) | XeLaTeX render service — compiles invoice/quote/statement PDFs for the web app. |
| [`saebooks-desktop`](https://github.com/saebooks/saebooks-desktop) | Native Qt desktop client (Windows/Linux/macOS). AGPLv3. |
| `saebooks-vault` | Document storage (receipts, invoices, contracts). Proprietary sibling service. |
| `saebooks-lodge-server` | ATO/SBR lodgement relay (signs + forwards STP/BAS envelopes). Proprietary. |
| `saebooks-feeds-server` | Bank-feeds relay to SISS/ACSISS. Proprietary. |
| `saebooks-license-server` | Mints Ed25519-signed licence tokens against subscription state. Proprietary. |

The engine and web frontend are open (AGPLv3 + commercial dual licence); hosted
SaaS infrastructure and certified e-lodgement engines are proprietary and never
published.

## Editions

| Edition | For |
|---|---|
| **Community** | Free, self-hosted, AGPLv3. The full core ledger. |
| **Pro** | Multi-company, FX revaluation, hosted convenience. |
| **Business** | STP payroll, fixed assets, ABR lookup. |
| **Enterprise** | Direct BAS e-lodgement and bank feeds via our DSP-accredited services. |

Commercial terms live in [SPEC-LICENSING.md](./SPEC-LICENSING.md) and the
Charter §6.

## Licence & trademark

- **Code:** AGPLv3 + commercial dual licence. AGPLv3's network-copyleft means a
  commercial fork can't run a modified SAE Books as proprietary SaaS without
  publishing its modifications. See [LICENSING.md](./LICENSING.md).
- **Trademark:** "SAE Books", "saebooks", and the SAE Books logo are trademarks
  of SAE Engineering. Forking the source under AGPL is fine; calling the fork
  "SAE Books" is not. See [TRADEMARK.md](./TRADEMARK.md).
- **Contributions:** a CLA is required from every external contributor from the
  first pull request. See [CLA.md](./CLA.md) and [CONTRIBUTING.md](./CONTRIBUTING.md).

## Links

- **Download — [saebooks.com.au/download](https://saebooks.com.au/download)**
- [Charter](./CHARTER.md) — strategic decisions, the senior doc
- Marketing — [saebooks.com.au](https://saebooks.com.au)
- Developer docs — [dev.saebooks.com.au](https://dev.saebooks.com.au)
- Community forum — [discourse.saebooks.com.au](https://discourse.saebooks.com.au)
- Estonia — [tasur.ee](https://tasur.ee)

## Contact

SAE Engineering · [saee.com.au](https://saee.com.au)
