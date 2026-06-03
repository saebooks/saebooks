# Deferred from feat/grpc-cli-mcp

The branch ships **API tokens**, **MCP server**, and **Go CLI** as the
AI-friendly surface. The following pieces from the original brief are
deferred to follow-up PRs because they are bigger than a single
overnight session can do responsibly:

## Connect-RPC mount (was tasks #3, #6, #7)

**Brief:** mount connecpy ASGI sub-app at `/saebooks.SAEBooks/*` inside
the existing FastAPI app to serve gRPC + gRPC-Web + plain HTTP+JSON
from one schema. Extend the proto with TimeEntries, Reports, Accounts,
Transactions, ApiTokens services.

**Why deferred:**

1. The existing standalone grpcio server at :50051 already covers
   Contacts, Invoices, Bills, Payments, JournalEntries with streaming
   (WatchChanges, WatchPresence, locks). Replacing it is a multi-hour
   refactor (handler extraction + connecpy proto codegen + ASGI mount
   + reflection wiring + all-handler migration + regression tests).
2. The MCP server is the *real* AI-friendly surface — agents call
   tools, not gRPC. Connect-RPC was the "cool kid" answer; MCP is the
   "least resistance for AI" answer.
3. The HTTP+JSON benefit of Connect is already served by the existing
   REST `/api/v1/*` endpoints. AI agents (and the MCP server tonight)
   can hit those today.
4. The mobile gRPC consumer (the original "build it as a forcing
   function" justification) is months out per
   `[[saebooks-mobile-architecture]]`. No active consumer pressure
   exists to validate the schema choices tonight.

**Resume conditions:**

- Mobile starts building → spawn a Connect-RPC PR with a clear schema
  contract.
- A third-party integration wants a typed wire format → ship Connect.
- Until then: REST + MCP cover every actual use case.

## Time entries / reports / accounts / transactions Connect handlers

Same reason — wait for the Connect mount.

## API token issuance via the Go CLI

The CLI ships `auth login` with a JWT-paste flow tonight. Real device-flow
or interactive OAuth issuance against the saebooks portal is follow-up.

For now: a user logs into the web UI, hits `POST /api/v1/api-tokens`,
copies the `saebk_*` cleartext, and pastes it into the CLI via
`sae books auth token import` (or sets `SAEBOOKS_TOKEN` env var).

## Per-scope authorization

**ENFORCED** (A2, `feat/api-token-scope-enforcement`). `ApiToken.scopes`
is now read for an authorization decision on every `saebk_*` API-token
request, in `require_bearer`'s API-token branch (`api/v1/auth.py`) via
`services/scopes.py`. Minimum-viable read/write matrix:

* `GET`/`HEAD`/`OPTIONS` require the `read` scope
* `POST`/`PUT`/`PATCH`/`DELETE` require the `write` scope (a `write`
  scope also satisfies `read`)

Backward-compatible: a token whose scopes are empty/`None` or carry a
full-access marker (`*`, `full`, or both `read` and `write`) keeps full
access exactly as before — so every previously-issued token (all issued
with the default `scopes=[]`) is unaffected. Only an explicitly
restrictive set (e.g. `["read"]`) is limited. Interactive JWT / session
/ web auth is untouched — it keeps role-based authz.

Follow-up (still deferred): a finer per-domain scope matrix
(e.g. `invoices.write` vs `payments.write`) if a consumer needs it.

## DNS / Caddy / Cloudflare for mcp.saebooks.com.au

The compose file ships with port 18313 bound on the LAN. Public DNS,
Cloudflare record, and OPNsense Caddy route are intentionally left to
manual review:

- Caddy route: `mcp.saebooks.com.au` → `10.0.2.1:18313`, TLS via LE
- Cloudflare DNS: A record → public IP, proxied=false (MCP needs raw
  SSE which CF can mangle)
- Authentik: bypass — MCP auth is bearer-only, not browser SSO

Will be done as a separate `caddy-add` + DNS-update pass after a
deploy smoke test.
