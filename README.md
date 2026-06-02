# SAE Books

> *Your books. Your database. Your control.*

**Self-hosted, API-first double-entry accounting for Australian small-to-medium
businesses — with a multi-market-ready core.**

SAE Books keeps your financial data in a Postgres database *you* control: your
laptop, your NAS, a VPS you rent, or our hosted offering. Same code everywhere.
The web UI is a thin client over a public REST API, so anything you can do in a
browser your scripts can do too — same endpoints, same auth, same OpenAPI schema.

**▶ Self-host it — [saebooks.com.au/self-host](https://saebooks.com.au/self-host)** · [Live demo](https://cashbook-demo.saebooks.com.au) · [Quickstart](https://dev.saebooks.com.au/getting-started/quickstart/)

## Status

| | |
|---|---|
| Phase | Private development — approaching first public beta |
| Public release | Not yet generally available |
| Primary user | SAE Engineering's own books, run live ahead of release |
| Licence | AGPLv3 + commercial dual licence (see [LICENSE](./LICENSE), [LICENSING.md](./LICENSING.md)) |
| Contributions | Closed until public; a CLA will be required (see [CLA.md](./CLA.md)) |

The [Charter](./CHARTER.md) is the senior document in this repository — it
captures the strategic decisions that shape SAE Books, and every engineering
doc is subordinate to it.

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
  contacts, banking, reconciliation, GST/BAS worksheets, and standard reports.
- **Public REST API** — the same endpoints the UI uses, documented via OpenAPI.
- **Multi-company** — multiple entities under one login, reciprocal journals,
  consolidated reports, row-level tenant isolation.
- **Period locks & audit ledger** — close a period, lock it, audit it; every
  change to a posted journal leaves a tamper-evident trail.
- **Immutable invoice snapshots** — the rendered PDF is stored byte-identical;
  reprintable forever, built for dispute defence.
- **FX revaluation** — multi-currency posting with end-of-period revaluation.
- **Purchase orders, fixed assets, ABR lookup, time tracking.**

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
| [`saebooks`](.) | The ledger engine — FastAPI + Postgres. AGPLv3 + commercial. |
| [`saebooks-web`](../saebooks-web) | Server-rendered frontend — FastAPI + Jinja2 + HTMX, no build step. A thin client over the API. AGPLv3 + commercial. |
| `saebooks-core-rs` | Shared Rust ledger core for native desktop/mobile (UniFFI → Swift/Kotlin). |
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

- [Charter](./CHARTER.md) — strategic decisions, the senior doc
- **Self-host & install — [saebooks.com.au/self-host](https://saebooks.com.au/self-host)**
- Marketing — [saebooks.com.au](https://saebooks.com.au)
- API docs — [dev.saebooks.com.au](https://dev.saebooks.com.au)
- Community — [discourse.saebooks.com.au](https://discourse.saebooks.com.au)

## Contact

SAE Engineering · [saee.com.au](https://saee.com.au)
