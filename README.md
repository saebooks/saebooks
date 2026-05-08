# SAE Books

> *Your books. Your database. Your control.*

**Self-hosted double-entry accounting for small-to-medium businesses.**

SAE Books is a free, open-source accounting platform you run on your own
hardware (or a VPS you own). It is built around an API-first ledger with
a server-rendered web UI and an optional desktop thin client. The
Community edition is licensed AGPL-3.0; commercial licences are
available for proprietary deployments.

> **Status:** v0.1 — public alpha. Expect rough edges. The ledger and
> web UI are usable today; we are looking for self-hosters who want to
> kick the tyres before the v1.0 cut.

## Try it (Linux / Docker)

```bash
git clone https://github.com/saebooks/saebooks.git
cd saebooks
cp .env.example .env
# Edit .env — at minimum set:
#   POSTGRES_PASSWORD
#   SAEBOOKS_FIELD_ENCRYPTION_KEY  (openssl rand -base64 32)
#   SAEBOOKS_SECRET_KEY            (openssl rand -base64 32)
#   SAEBOOKS_WEB_SECRET_KEY        (openssl rand -base64 32)
#   SAEBOOKS_SQL_RO_PASSWORD       (openssl rand -base64 24 | tr -d '/+=' | head -c 32)
docker compose up -d

# Mint the first owner + a bearer token:
docker compose exec api \
  python -m saebooks.cli bootstrap-admin --email you@example.com
```

This pulls pre-built `saebooks/saebooks` and `saebooks/saebooks-web`
images from Docker Hub. To build from source instead, clone
`saebooks-web` as a sibling directory and use
`deploy/sap/docker-compose.yml`.

| Surface | URL |
|---|---|
| Web UI | <http://localhost:8080> |
| REST API | <http://localhost:8042> |
| gRPC (desktop client) | `localhost:50051` |

For the full self-hosting walkthrough — TLS, Postgres tuning, backups,
upgrades — see [SELFHOST.md](./SELFHOST.md).

### Hosted Community

If you'd rather not run anything yourself, a hosted Community tier is
landing at <https://saebooks.com.au> — sign up for the alpha there.

### Windows

A Windows all-in-one installer (bundled Postgres + API + web) is
published with the v0.1 tag — see the [Releases](../../releases) page.

## What's in the box

| Module | Status |
|---|---|
| Chart of accounts (multi-currency) | working |
| Customers, vendors, items | working |
| Invoices, bills, credit notes | working |
| Journal entries (double-entry, RLS-isolated per tenant) | working |
| Bank accounts + manual reconciliation | working |
| Bank-feed ingestion | feature-flagged (Pro) |
| Fixed-asset register + depreciation | feature-flagged (Business) |
| AU GST, BAS report generation | working |
| AU Single Touch Payroll lodgement | feature-flagged (Pro) |
| AI document extraction (vision LLM via OpenAI-compatible API) | feature-flagged (Pro) |
| Multi-company within a tenant | feature-flagged (Business) |
| Reports: P&L, Balance Sheet, Trial Balance, Cash Flow | working |
| Desktop thin client (`saebooks-desktop`) | alpha |

## Editions

A single binary; behaviour gated by `SAEBOOKS_EDITION`:

```
community ⊂ offline ⊂ business ⊂ pro ⊂ enterprise
```

* **community** (default, AGPL) — full ledger, single company, single
  tenant, no commercial integrations.
* **offline** — Community plus offline-first sync features.
* **business** — multi-company, fixed-asset register, advanced
  reporting.
* **pro** — bank feeds, STP lodgement, AI extraction.
* **enterprise** — custom; talk to us.

Editions above Community require a licence key from SAE Engineering.
The Community edition is fully usable on its own; the higher tiers
unlock features but do not gate the core ledger.

## Architecture

* **API** (`saebooks/`) — FastAPI + async SQLAlchemy 2.x + asyncpg +
  Postgres 16 + Alembic. REST + gRPC. AGPL-3.0.
* **Web** (`saebooks-web/`) — Jinja2 + HTMX server-rendered UI talking
  to the API over HTTPS. AGPL-3.0.
* **Desktop** (`saebooks-desktop/`) — cx_Freeze thin client for
  Windows/Linux. Talks gRPC to the API. AGPL-3.0.

The web layer is a separate process on a separate port; the API is the
licensed product. You can run the API alone if you only want the REST
surface, or swap the web layer for your own front end.

## Docs

* [CHARTER.md](./CHARTER.md) — strategic decisions, scope boundaries,
  what this is and isn't.
* [LICENSING.md](./LICENSING.md) — AGPL vs commercial licence terms.
* [CLA.md](./CLA.md) — contributor licence agreement.
* [TRADEMARK.md](./TRADEMARK.md) — what you can and can't call your
  fork.
* [docs/](./docs/) — design docs, edition rationale, integration
  guides.

## Known limitations (v0.1)

* The web layer currently runs against a Postgres role with
  `BYPASSRLS` — row-level security is enforced at the API, not the DB
  session. This is documented and on the roadmap for v0.2.
* AU-specific (GST, BAS, STP, ABR enrichment); other jurisdictions need
  a custom chart of accounts and tax-code set.
* No double-entry-aware import yet for QuickBooks/Xero/MYOB customers
  beyond CSV.
* Single-node deployments only; no built-in HA story.

## Contributing

Public alpha contributions are open. Read
[CONTRIBUTING.md](./CONTRIBUTING.md) and [CLA.md](./CLA.md) before
opening a PR.

Security issues: <security@saee.com.au>. Please do not file public
issues for vulnerabilities.

## Licence

AGPL-3.0 (see [LICENSE](./LICENSE)). Commercial licences available —
see [LICENSING.md](./LICENSING.md) or contact <sales@saee.com.au>.

---

SAE Engineering · <https://saee.com.au>
