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

`ApiToken.scopes` is informational only — server-side scope enforcement
is TODO. Every token currently has the user's full role-level access.
Tracked as a follow-up.

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
