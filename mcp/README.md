# saebooks-mcp

HTTP MCP server for SAE Books. Exposes the SAE Books REST API as
native AI-agent tools so Claude Code (and any other MCP client) can
interact with invoices, bills, expenses, journals, contacts, bank
reconciliation, projects, accounts, time tracking, and reports
without subprocess shelling.

## Tool inventory

145 tools as of v0.2, organised by safety level. The
`SAEBOOKS_MCP_MAX_SAFETY` env var caps which tools are registered at
startup.

| level | count | examples |
|---|---:|---|
| `safe` | 60 | `whoami`, `get_openapi_schema`, all `list_*`/`get_*`, all reports, `search`, `recent_changes` |
| `mutation` | 58 | `create_invoice`, `update_invoice`, `post_invoice`, `record_payment`, `match_bank_statement_line`, `reconciliation_auto_match`, `create_journal_entry`, `update_time_entry` |
| `void` | 14 | `void_invoice`, `void_bill`, `archive_contact`, `archive_project`, `archive_quote`, `delete_attachment`, `archive_time_entry` |
| `destructive` | 13 | `hard_delete_contact`, `delete_invoice` (posted), `reverse_journal_entry`, `lodge_bas`, `call_api` (raw API escape hatch) |

The cap is **monotonic** — setting `MAX_SAFETY=void` registers safe +
mutation + void (132 tools) and skips destructive. Setting it to
`safe` gives a read-only assistant (60 tools).

`destructive` is the default cap (everything registered) so existing
single-tenant deployments don't break on upgrade.

## Auth

Every MCP client supplies their own SAE Books API token
(`saebk_*`) via the MCP transport's `Authorization` header. Issue
tokens at `/admin/api-tokens` (browser session required).

For single-tenant deployments, you can also set `SAEBOOKS_API_TOKEN`
in the env — every tool call then uses that fallback token. This is
how the bundled `saebooks-mcp` and `saebooks-mcp-community` compose
stacks below auth.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `SAEBOOKS_API_URL` | `http://api:8000` | Base URL of the SAE Books REST API |
| `SAEBOOKS_API_TOKEN` | (empty) | Shared fallback `saebk_*` token |
| `SAEBOOKS_MCP_MAX_SAFETY` | `destructive` | Cap on tool registration: `safe` / `mutation` / `void` / `destructive` |
| `SAEBOOKS_MCP_TRANSPORT` | `sse` | `sse` / `streamable-http` / `stdio` |
| `SAEBOOKS_MCP_HOST` | `0.0.0.0` | Bind address |
| `SAEBOOKS_MCP_PORT` | `8000` | Listen port |
| `SAEBOOKS_MCP_LOG_LEVEL` | `INFO` | Python logging level |
| `SAEBOOKS_MCP_DISABLE_HOST_CHECK` | `1` | Disable FastMCP's DNS-rebinding guard (set to `0` if not running behind a TLS edge) |

## Editions

Two compose stacks ship with this repo:

| stack | safety cap | port | for |
|---|---|---:|---|
| `mcp/` | `destructive` | 18314 | operator (full control, including BAS lodgement and hard-delete) |
| `mcp-community/` | `void` | 18316 | regular users (everything except hard-delete-posted, reverse-JE, `lodge_bas`, raw `call_api`) |

Both stacks point at the same underlying API instance and use the
same `saebk_*` token — the safety split is purely a tool-surface
gate. The API still enforces its own role-based authorization
independently.

### Why two stacks instead of one with a header switch

The MCP transport doesn't (yet) let a client say "I want the
restricted tool surface, please" — what tools exist is decided at
container start. So we run two containers. Switching editions = point
your MCP client at the other URL.

## Register in Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "saebooks": {
      "transport": "sse",
      "url": "http://10.0.2.1:18314/sse",
      "headers": {
        "Authorization": "Bearer saebk_..."
      }
    },
    "saebooks-community": {
      "transport": "sse",
      "url": "http://10.0.2.1:18316/sse",
      "headers": {
        "Authorization": "Bearer saebk_..."
      }
    }
  }
}
```

Use `saebooks` from your trusted operator session, `saebooks-community`
when handing the surface to a less-trusted agent or onboarding a new
bookkeeper.

## `call_api` escape hatch

The full edition exposes `call_api(method, path, body, params, if_match, idempotency_key)`
at `safety=destructive`. This is for the small set of endpoints not
yet wrapped in a dedicated tool — combine it with
`get_openapi_schema` to discover what's possible.

Community edition omits `call_api` deliberately — if it were
exposed, all the safety gating could be bypassed by issuing the
request directly.

## Versioning

- **v0.1** — 23 tools, hand-rolled, no safety levels.
- **v0.2** (current) — 145 tools, four safety tiers, `call_api`
  escape hatch, `get_openapi_schema` introspection, community
  variant. Backwards-compatible — default cap is `destructive` so
  existing clients see the full surface.
