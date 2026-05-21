# saebooks-mcp

HTTP MCP server for SAE Books. Exposes the SAE Books REST API as
native AI-agent tools so Claude Code (and any other MCP client) can
interact with invoices, contacts, journal entries, and reports
without subprocess shelling.

## Tools

- `whoami` — identify the active user/company
- Contacts: `list_contacts`, `get_contact`, `create_contact`
- Invoices: `list_invoices`, `get_invoice`
- Bills: `list_bills`
- Journal entries: `list_journal_entries`, `get_journal_entry`
- Accounts: `list_accounts`
- Reports: `trial_balance`, `profit_and_loss`, `balance_sheet`,
  `aged_receivables`, `aged_payables`
- `search` — global free-text search

## Auth

Every MCP client supplies their own SAE Books API token (`saebk_*`)
via the MCP transport's Authorization header. Issue tokens at
`POST /api/v1/api-tokens` (browser session required — JWT bootstrap).

For single-tenant dev deployments, you can also set
`SAEBOOKS_API_TOKEN` in the env — every tool call then uses that
fallback token.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `SAEBOOKS_API_URL` | `http://api:8000` | Base URL of the SAE Books REST API |
| `SAEBOOKS_API_TOKEN` | (empty) | Optional shared fallback token |
| `SAEBOOKS_MCP_TRANSPORT` | `sse` | `sse` / `streamable-http` / `stdio` |
| `SAEBOOKS_MCP_HOST` | `0.0.0.0` | Bind address |
| `SAEBOOKS_MCP_PORT` | `8000` | Listen port |
| `SAEBOOKS_MCP_LOG_LEVEL` | `INFO` | Python logging level |

## Register in Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "saebooks": {
      "transport": "sse",
      "url": "https://mcp.saebooks.com.au/sse",
      "headers": {
        "Authorization": "Bearer saebk_..."
      }
    }
  }
}
```
