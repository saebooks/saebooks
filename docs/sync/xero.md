# Xero sync

Status: alpha. Enterprise-tier feature, gated by both `FLAG_ACCOUNTING_SYNC`
and `FLAG_SYNC_XERO`. Disabled by default in all editions.

This document covers the operator-facing surface, the data flow, and the
operational/runtime contract for the Xero adapter under
`saebooks/services/sync/xero/`.

---

## Scope

The adapter syncs three object types between SAE Books and a single Xero
organisation per `sync_connection`:

| Object | Direction | Notes |
|--------|-----------|-------|
| Contacts | bi-directional | All `IsCustomer`/`IsSupplier` combinations supported |
| Invoices (`ACCREC`) | bi-directional | AR — customer invoices |
| Invoices (`ACCPAY`) | bi-directional | AP — supplier bills |
| Manual Journals | one-shot push | `push_journal` — used to mirror accountant adjustments |

Out of scope for v1: bank feeds (separate ACSISS/SISS pipeline), payments
allocation, fixed-asset register, payroll.

## Architecture

```
saebooks/services/sync/xero/
├── client.py        # XeroClient — HTTP transport, retries, 401/429 handling
├── token.py         # XeroTokenCache, build_authorize_url, exchange_code_for_tokens
├── endpoints.py      # Thin per-endpoint wrappers (Contacts, Invoices)
├── mappers.py        # Xero JSON ↔ SAE Books dataclass mappers
├── pull.py           # pull_contacts, pull_invoices — incremental, watermarked
├── push.py           # push_contacts, push_invoices, push_journal — LWW
└── connector.py       # sync_xero — top-level per-connection orchestrator
```

### Token cache

`XeroTokenCache` holds the access token in memory and refreshes it lazily
when expired or on a 401. **Refresh-token rotation is mandatory** — Xero
returns a new refresh token on every refresh and the old one is revoked
on the next call. The cache exposes an `on_refresh_rotated(new_refresh)`
callback; `connector.sync_xero` uses this to re-encrypt and persist the
new refresh token under the same DB transaction that wraps the run, so a
crashed sync never loses the rotated credential.

### HTTP client

`XeroClient.request()` is the single point of HTTP egress and handles:

* Bearer + `Xero-tenant-id` header injection
* `If-Modified-Since` for incremental pulls (RFC 7232 HTTP-date)
* `401` → force-refresh once → retry once → if still 401, raise
  `SyncAuthError` (translated to `connection.status = revoked` upstream)
* `429` → honour `Retry-After`, retry up to 3x; final 429 raises
  `SyncRateLimited` for a future scheduled poll to re-run against
* `5xx` → raises `SyncUpstreamError` (transient — connection stays `active`)

All other 4xx surface as `SyncValidationError` with the response body in
the message.

### Pull flow

`pull_contacts` / `pull_invoices`:

1. Read `connection.last_pulled_at`. Pad it back by 1 second
   (`_ifms_with_one_second_pad`) to avoid losing rows whose
   `UpdatedDateUTC` equals the watermark.
2. Page through Xero with `If-Modified-Since: <padded watermark>`.
3. For each row:
   * Look up local row by `external_id`.
   * Detect conflict: `state.last_pushed_version` set AND
     `local.version > state.last_pushed_version` means we have an
     unsynced local change. Outcome `conflict`, write
     `sync_audit_log`, **do not** apply the remote.
   * Otherwise upsert. `_merge_contact_type` and `_merge_invoice_status`
     enforce monotonic invariants (BOTH never demotes, POSTED never
     reverts to DRAFT, VOIDED is terminal).
   * Persist `sync_state` row with `last_pulled_etag` =
     `UpdatedDateUTC` and `last_pulled_at` = now.
4. Advance `connection.last_pulled_at` to the **server-side maximum
   `UpdatedDateUTC`** seen this pass (NOT `datetime.now()`) — protects
   against clock drift between SAE Books and Xero.

If a pulled invoice references a contact whose local row doesn't yet
exist, the invoice is **quarantined** (`sync_state.quarantined = true`)
for the next pass. The next `pull_contacts` brings the contact in;
`pull_invoices` then dequarantines and applies. This avoids forced
ordering inside a single pass.

A pulled invoice never gets a `journal_entry_id` — the header-only
insert bypasses the normal `services/journal.py` posting flow entirely
(there is no Xero-side GL journal to mirror per invoice). Reports that
assume every `POSTED` invoice has a `journal_entry_id` should treat
pulled Xero invoices as an exception.

### Push flow

`push_contacts` / `push_invoices`:

1. Candidates: rows where `version > last_pushed_version` OR
   `external_id IS NULL`. Invoices restricted to `status = POSTED` —
   DRAFT invoices stay local.
2. For each candidate:
   * If `sync_state.last_pulled_etag` differs from the row's
     `external_etag` from a fresh upstream GET, declare a conflict —
     remote moved since we last pulled. Quarantine and audit.
   * Otherwise POST `Contacts` / `Invoices` with the row body.
   * On success: persist `external_id`, `external_source = "xero"`, and
     `sync_state.last_pushed_version = local.version`.
3. The pure helper `detect_conflict(state, local_version,
   current_remote_etag)` is the explicit predicate — both sides moved
   since the last successful sync = conflict, otherwise not.

Invoices are **header-only** updates after the first push. Lines are
never edited remotely. This honours the invoice-immutability invariant
([`saebooks-marketing-differentiator`]) — the rendered PDF
snapshot is the product, not a bug to work around.

### Connector — `sync_xero(session, *, connection)`

Top-level entry point. Used by `POST /api/v1/sync/xero/{id}/trigger`
(operator-initiated). A scheduled background worker is future work —
not yet wired.

Order:

1. `pull_contacts`
2. `pull_invoices(ACCREC)`
3. `pull_invoices(ACCPAY)`
4. `push_contacts`
5. `push_invoices`

Tenancy guard: asserts `app.current_tenant` GUC matches
`connection.tenant_id`. The trigger router is responsible for setting
the GUC and holding `SELECT ... FOR UPDATE` on the connection row so
concurrent runs can't race on the same refresh token.

`SyncAuthError` from any step → `connection.status = revoked` and stop.
`SyncError` → `connection.status = error`, `last_error` set, return.
`SyncRateLimited` is re-raised — a future worker would re-schedule.

A summary of the run is appended to `sync_audit_log` regardless of
outcome.

---

## Operator surface (`/api/v1/sync/xero`)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/connect` | Mint a state token and PKCE pair, persist client_id/secret, return `authorize_url` |
| `GET` | `/callback` | OAuth redirect target — exchange code, fetch orgs, mark connection `active` |
| `GET` | `/status` | List the tenant's Xero connections + last sync summary |
| `DELETE` | `/{id}` | Mark `revoked`, wipe refresh token |
| `POST` | `/{id}/trigger` | Run one full sync cycle synchronously |

Multi-org consent (operator picks among several Xero orgs at consent
time) is deferred to a follow-up build — v1 takes the first org returned
by `GET https://api.xero.com/connections`.

### Web UI

A future `saebooks-web` `/settings/integrations` "Xero" panel would:

* **Disconnected** state: form for `client_id`, `client_secret`,
  `redirect_uri`. Submitting POSTs to `/api/v1/sync/xero/connect` and
  redirects the operator's browser to `authorize_url`.
* **Connected** state: shows the Xero org name, `last_pulled_at`,
  `last_pushed_at`, last sync outcome. Buttons for "Sync now" (POST
  `/trigger`) and "Disconnect" (DELETE).

This UI is not built as of this port — the API surface above is the
complete v1 entry point.

---

## Hard-delete guard

Admins MUST be able to hard-delete sync-linked rows, but silently
dropping a Xero-linked invoice is unsafe — the row still exists
upstream and resurrects on the next pull.

`saebooks/services/hard_delete.py::check_sync_state_or_force` enforces:

> If a `sync_state` row exists for this object on any **active** sync
> connection, the caller must pass `force=True` (which carries the
> operator's explicit confirmation header `X-Confirm-Hard-Delete-
> Synced: yes`).

Without `force`, the helper raises `HardDeleteSyncedError` — a router
would map this to HTTP 409 with the connection IDs in the body so the
UI can show "this row is linked to Xero — confirm to delete".

Sync-eligible tables: `contacts`, `invoices`, `bills`, `payments`,
`credit_notes`, `journal_entries`. Anything else (users, account
ranges, etc.) bypasses the guard.

**Port caveat:** `hard_delete_with_audit`'s `force_sync_override` kwarg
exists and defaults to `False` (so sync-eligible tables are guarded by
default), but none of the ~28 existing hard-delete route call-sites
pass a `force_sync_override` value or read the
`X-Confirm-Hard-Delete-Synced` header yet — wiring that header through
each route is follow-up work, not part of this port (it wasn't wired
on the origin branch either).

---

## Testing strategy

All HTTP egress is mocked via `respx`. The test suite covers:

* `test_token.py` — refresh, rotation, on_refresh_rotated callback,
  authorize URL construction, code exchange. Pure/respx — runs on
  SQLite.
* `test_client.py` — 401-once-then-refresh, 429 + Retry-After,
  If-Modified-Since header injection. Pure/respx — runs on SQLite.
* `test_endpoints.py` — pagination (`page=1,2,…`), filter parameters
  (`Type=="ACCREC"`, `where`). Pure/respx — runs on SQLite.
* `test_mappers.py` — Xero JSON ↔ dataclass round-trips for contacts
  and invoices, including phone concatenation, address types, and
  status enums. Pure — runs on SQLite.
* `test_pull.py` — end-to-end DB pull: insert new contact, advance
  watermark, append audit row. **Postgres-only** (`SET LOCAL
  app.current_tenant` + RLS).
* `test_push.py` — end-to-end DB push: candidate selection, external_id
  persistence, sync_state row write. **Postgres-only**.
* `test_connector.py` — full pull+push round-trip smoke test.
  **Postgres-only** (`connector.sync_xero` runs `SHOW
  app.current_tenant`, which SQLite doesn't support).
* `test_hard_delete_guard.py` — guard fires on synced row, returns
  silently with `force=True`, ignores non-sync-eligible tables.
  **Postgres-only**.

**No test ever hits live Xero.** The respx fixtures are recorded by
hand from the [Xero API documentation](https://developer.xero.com/).

Run the suite:

```
pytest tests/services/sync/ -v
```

Or the SQLite-safe subset only:

```
pytest tests/services/sync/xero/test_mappers.py \
       tests/services/sync/xero/test_token.py \
       tests/services/sync/xero/test_client.py \
       tests/services/sync/xero/test_endpoints.py -v
```

---

## Operational notes

* **Cadence.** No scheduled worker exists yet — the trigger endpoint is
  the only execution path (operator's "sync now" button).
* **Concurrency.** `SELECT ... FOR UPDATE` on the connection row inside
  the trigger endpoint. Two concurrent runs on the same connection
  would race on refresh-token rotation and lose one.
* **Rotation.** Xero refresh tokens expire after 60 days of inactivity.
  Tenants that don't sync for 60+ days will hit `revoked` on the next
  attempt and need to re-OAuth.
* **Multi-org tenants.** A SAE Books tenant currently maps to one Xero
  org via `external_tenant_id`. Multiple Companies on the same tenant
  all sync to the same org — the connector resolves `company_id` from
  the tenant's first Company by `created_at`. When a multi-company-
  per-org requirement appears, add `connection.company_id` and switch
  the resolver.

---

## Future work

* Multi-org consent flow (operator picks from > 1 org).
* Background scheduled worker — currently the trigger endpoint is the
  only execution path.
* Web UI (`/settings/integrations` Xero panel) — not built.
* Bank feeds via ACSISS/SISS — not part of this adapter.
* Account / chart-of-accounts pull (read-only mirror for reporting).
* Tracking categories.
* `sync_coa_account_request` / a trigger-on-miss CoA resolver — the
  table exists (schema-only, migration 0217) but no consumer was ever
  implemented.
