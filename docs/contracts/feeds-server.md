# saebooks-feeds-server — HTTP contract

> **Authoritative.** Consumed by `RemoteBankFeedsService` in
> saebooks-api (Cat-C / W4). Lock this contract before implementing
> producers or consumers.
>
> Sister contract: `~/.claude/plans/saebooks-lodge-server-contract.md`.
> feeds-server intentionally mirrors the lodge-server shape — same
> auth model, same audit-row discipline, same stub-mode policy — so
> a developer who knows one knows the other.

## Deployment shape

- **Hostname:** `feeds.saebooks.com.au`
- **Runtime:** FastAPI + uvicorn behind OPNsense Caddy, on r420 today.
  Python (NOT Rust) — license-server is Rust because it's the hot
  path; feeds-server is a thin relay and the SISS / ACSISS SDK is
  Python-first.
- **Host port:** `10.0.2.1:18313:8000` (LAN-only bind; Caddy on
  OPNsense reaches us at `10.0.2.1:18313`).
- **Storage:** dedicated Postgres database `saebooks_feeds` on the
  shared `bosun-postgres` (`postgresql18`) instance.
- **Repos:**
  - gitea (working): `http://10.0.2.1:3031/sauer/saebooks-feeds-server`
  - github (canonical): `git@github.com:saebooks/saebooks-feeds-server`
- **Secrets** (real values in `~/.claude/secrets/saebooks-feeds-server.env`,
  mode 0600, never committed):
  - `SAEBOOKS_PORTAL_PUBKEY` — Ed25519 pubkey, raw 32-byte base64;
    same key as embedded in saebooks-api and lodge-server.
  - `DATABASE_URL` — `postgresql+asyncpg://feeds:<pw>@postgresql18:5432/saebooks_feeds`.
  - `FEEDS_TENANT_ID` — defaults to `sae-engineering` (single-tenant
    relay, but every audit row carries it for forward compatibility).
  - `SISS_BASE_URL` / `SISS_CLIENT_ID` / `SISS_CLIENT_SECRET` /
    `SISS_SUBSCRIPTION_KEY` — TBD when SISS Data Services onboarding
    completes (see `[[bank-feeds]]` memory). Build-stub does NOT
    need these set; they are commented in `.env.example`.
- **Network:** outbound to SISS endpoints (`https://*.acsiss.com.au`,
  `https://*.sissdataservices.com.au` — exact hosts TBD on onboarding).
  Inbound HTTPS only.

## Auth model

Mirrors lodge-server exactly. Every feeds-server request includes a
customer licence token (JWT signed by license-server with EdDSA /
Ed25519) in the `Authorization: Bearer <token>` header. Server:

1. Verifies signature with `SAEBOOKS_PORTAL_PUBKEY`.
2. Checks `exp` (with leeway) and honours `grace_until` claim.
3. For all routes except `/healthz`: requires `feeds_enabled` to
   be true. The flag falls out of the licence's edition tier:
   - `community`, `offline` -> `feeds_enabled = false` (gated, 403).
   - `business`, `pro`, `enterprise` -> `feeds_enabled = true`.
   - The licence-server is the canonical source — when it mints a
     token it sets `feeds_enabled` on the payload directly. The
     server here also does a defensive fallback: if `feeds_enabled`
     is missing, edition in {business, pro, enterprise} is treated
     as true. This lets license-server roll out gradually.
4. Records `(license_id, jti, route, ts, request_hash, response_hash,
   status, latency_ms)` in audit log on every request that passed
   auth, irrespective of whether the upstream SISS call succeeded.

No additional auth — the licence token IS the auth. No Authentik in
front of this service.

### Why a JWT *claim* instead of a hard-coded edition table

`feeds_enabled` is the contract surface so that license-server can
later flip the gate per-customer (eg trial users on Community, holdouts
on Enterprise without feeds) without a feeds-server release. Same
discipline as lodge-server's STP/BAS gate via `edition`.

## Routes

All paths are versioned under `/api/v1/`. Path versioning per the
infrastructure plan §"Contracts are immutable; bump the path on a break".
Adding new claims is backwards-compatible; renaming or making a field
required = bump major.

### `POST /api/v1/connections`

Initiate a bank-feed enrolment (consent flow). Customer-facing UX
in saebooks-web kicks off here, gets back a hosted-consent URL,
redirects the customer's browser to SISS. SISS redirects back into
saebooks (not into feeds-server) with a code; saebooks-api exchanges
the code by calling `POST /api/v1/connections/{id}/finalise` (TBD —
not in stub-mode v1; see "Out of scope for v1").

**Request:**
```json
{
  "ledger_id": "ledger-uuid",
  "redirect_uri": "https://app.saebooks.com.au/bank-feeds/callback",
  "institution_id": "AU000001",
  "metadata": {
    "ledger_label": "Sauer Pty Ltd",
    "operator_user_id": "user-uuid"
  }
}
```

- `ledger_id` — saebooks-side ledger UUID; bound at creation, can't
  be changed afterwards.
- `redirect_uri` — must match a redirect URI registered with SISS at
  client registration. Validated client-side; server records it in
  the audit row but does not attempt to validate it (SISS rejects on
  the consent screen if mismatched).
- `institution_id` — SISS / CDR institution code. Optional in v1.
- `metadata` — passthrough; persisted to the connection row for audit.

**Response (Build-stub, 501):**
```json
{
  "status": "stub",
  "would_have_initiated": true,
  "stub_connection_id": "stub_conn_<uuid>",
  "stub_consent_url": "https://stub.saebooks.com.au/feeds-stub/consent?conn=stub_conn_<uuid>",
  "comment": "feeds-server is stubbed. SISS / ACSISS Data Services onboarding pending — see bank-feeds memory."
}
```

**Response (live, 201):**
```json
{
  "status": "pending_consent",
  "connection_id": "conn_<uuid>",
  "consent_url": "https://acsiss.com.au/consent/...",
  "expires_at": "2026-05-04T12:00:00Z"
}
```

Status codes:
- `201` — connection created, consent URL returned.
- `400` — malformed body / unknown institution_id.
- `401` — missing or invalid licence token.
- `403` — `feeds_enabled` false (edition gate).
- `409` — duplicate `(license_id, ledger_id, institution_id)` already
  has an active connection. Body includes the existing `connection_id`
  so the caller can recover.
- `501` — stub-mode (Build-stub).
- `502` — SISS upstream returned 5xx / refused the request.
- `503` — SISS unreachable (transport error).

### `GET /api/v1/connections`

List the calling licence's connections.

**Query params:**
- `ledger_id` (optional) — filter to a single ledger.
- `status` (optional) — `pending_consent` | `active` | `revoked` |
  `expired`. If absent, returns all.
- `limit` (default 100, max 500).

**Response (live, 200):**
```json
{
  "license_id": "lic_xyz",
  "rows": [
    {
      "connection_id": "conn_<uuid>",
      "ledger_id": "ledger-uuid",
      "institution_id": "AU000001",
      "status": "active",
      "consent_expires_at": "2027-05-04T00:00:00Z",
      "last_sync_at": "2026-05-04T01:00:00Z",
      "last_sync_cursor": "opaque-token",
      "created_at": "2026-05-04T00:00:00Z"
    }
  ]
}
```

**Response (Build-stub, 200):**
```json
{
  "license_id": "lic_xyz",
  "rows": []
}
```

The stub returns an empty list (rather than 501) because callers
need to be able to render an empty state without special-casing
stub-mode. Stub-mode is signalled by `/healthz.stub_mode = true`.

Status codes: `200`, `401`, `403`.

### `GET /api/v1/connections/{connection_id}`

Fetch a single connection. 404 if not owned by the calling licence
(deliberately conflated with not-found to avoid licence-fishing).

**Response (live, 200):** same row shape as the list endpoint, single
object (not wrapped in `rows`).

**Response (Build-stub, 501):** the deterministic stub body. Stub-mode
returns 501 here because callers reaching for a specific connection
that doesn't exist need to know the difference — they can't render an
empty state for a single-resource fetch.

Status codes: `200`, `401`, `403`, `404`, `501`.

### `DELETE /api/v1/connections/{connection_id}`

Revoke a connection. In live mode: cascades a revoke to SISS, marks
the row `status = revoked`, retains the row for audit.

**Response (live, 200):**
```json
{
  "connection_id": "conn_<uuid>",
  "status": "revoked",
  "revoked_at": "2026-05-04T02:00:00Z"
}
```

**Response (Build-stub, 501):** the deterministic stub body.

Status codes: `200`, `401`, `403`, `404`, `501`, `502`, `503`.

### `POST /api/v1/transactions/sync`

Pull new transactions for a connection (or all connections of the
calling licence if no `connection_id` is supplied). In live mode this
is the workhorse — calls SISS, paginates, returns a normalised
transaction list plus the new cursor.

**Request:**
```json
{
  "connection_id": "conn_<uuid>",
  "since_cursor": "opaque-token-or-null"
}
```

If `connection_id` is absent, sync is fanned out across all the
licence's active connections. `since_cursor` is the value returned
by the previous sync; null/absent = since the last persisted cursor
on the connection row.

**Response (live, 200):**
```json
{
  "connection_id": "conn_<uuid>",
  "transactions": [
    {
      "external_id": "siss-txn-id",
      "posted_at": "2026-05-03T14:22:00Z",
      "amount_cents": -4500,
      "currency": "AUD",
      "description": "WOOLWORTHS 1234",
      "running_balance_cents": 123456,
      "raw": { "_": "SISS payload, redacted of PII" }
    }
  ],
  "next_cursor": "opaque-token",
  "has_more": false
}
```

**Response (Build-stub, 501):**
```json
{
  "status": "stub",
  "would_have_synced": true,
  "stub_sync_id": "stub_sync_<uuid>",
  "comment": "feeds-server is stubbed. SISS / ACSISS Data Services onboarding pending — see bank-feeds memory."
}
```

Idempotency: the `Idempotency-Key` header (24h replay window — see
"Idempotency" below) lets callers safely retry a sync that timed out
mid-flight without double-charging the SISS call quota. Same key
within 24h returns the cached prior response.

Status codes: `200`, `400`, `401`, `403`, `404` (unknown
connection_id), `409` (idempotency conflict — same key, different
body hash), `501`, `502`, `503`.

### `GET /healthz`

Standard health probe. Open to anyone (no auth).

**Response (200):**
```json
{
  "status": "ok",
  "service": "feeds-server",
  "version": "<git_sha>",
  "stub_mode": true
}
```

## Idempotency-Key header

Mirrors lodge-server's `payevent_id` semantics but uses the standard
HTTP header form (lodge-server bakes the id into the body because
the SBR envelope already needs a per-envelope identifier; feeds
routes don't, so we use the header).

- **Header name:** `Idempotency-Key`. Opaque string, max 128 chars.
- **Scope:** `(license_id, route, idempotency_key)` is the unique
  index. Same key under a different route is a different operation.
- **Replay window:** 24 hours from first observation. After that the
  key is considered fresh again.
- **Replay behaviour:** the second request returns the cached
  response body and status code from the first. The server does NOT
  re-run the upstream call.
- **Conflict:** if the second request has the same key but a
  different request body hash (`sha256(canonical_json(body))`), return
  `409 Conflict` with body
  ```json
  {"detail": "Idempotency-Key reused with different request body",
   "first_request_hash": "...", "this_request_hash": "..."}
  ```
- **Routes that honour it:** `POST /connections`, `DELETE /connections/{id}`,
  `POST /transactions/sync`. `GET` routes do not need it (idempotent
  by definition).

When `Idempotency-Key` is absent the server generates an internal
opaque key for the audit row but does not persist it as a replay
target. So a missing header still gives a clean audit trail; it just
means the caller bears the responsibility for retry safety.

## Audit row schema

Every request that passed auth + edition gate gets one row in
`feeds_audit`, regardless of whether the upstream SISS call succeeded.
Per the SAE Books infrastructure plan §4 — auditable from day 1.

| column           | type           | notes |
|------------------|----------------|-------|
| `id`             | bigserial PK   | |
| `tenant_id`      | varchar(64)    | defaults `sae-engineering`. |
| `request_id`     | varchar(64)    | a fresh ULID per request, echoed in `X-Request-ID` response header. |
| `license_id`     | varchar(64)    | from licence claim. |
| `jti`            | varchar(64)    | from licence claim, nullable. |
| `edition`        | varchar(32)    | from licence claim. |
| `ledger_id`      | varchar(64)    | from request body when present, nullable for list/health. |
| `route`          | varchar(64)    | logical name, eg `connections.create`, `transactions.sync`. |
| `idempotency_key`| varchar(128)   | nullable — null when header absent. |
| `request_hash`   | varchar(128)   | sha256 hex of `canonical_json(request_body)`; null for GET. |
| `response_hash`  | varchar(128)   | sha256 hex of response body. |
| `status`         | smallint       | HTTP status code returned. |
| `upstream_status`| smallint       | nullable — SISS HTTP status when applicable. |
| `latency_ms`     | integer        | request -> response wall-clock. |
| `client_ip`      | varchar(64)    | first hop of `X-Forwarded-For`, else `request.client.host`. |
| `error_detail`   | text           | nullable — short error message when `status >= 400`. |
| `ts`             | timestamptz    | server clock at write. |

Indexes: `(license_id, ts desc)`, `(license_id, idempotency_key)`,
`(route, ts desc)`, `(ts desc)` for retention sweeps.

Retention: bank-feed audit rows retained per the SISS contract terms
(typically 7 years for AU banking records — confirm at SISS
onboarding). Build-stub stores everything; retention sweeps are a
later concern.

## Idempotency table

Separate from the audit log — idempotency is keyed for fast lookup
on the hot path; audit is append-only and queried offline.

| column            | type         | notes |
|-------------------|--------------|-------|
| `id`              | bigserial PK | |
| `tenant_id`       | varchar(64)  | |
| `license_id`      | varchar(64)  | |
| `route`           | varchar(64)  | logical name; same labels as audit row. |
| `idempotency_key` | varchar(128) | |
| `request_hash`    | varchar(128) | sha256 hex of canonical request body. |
| `response_body`   | jsonb        | the cached response. |
| `status_code`     | smallint     | the cached HTTP status. |
| `ts`              | timestamptz  | first-seen. Sweep at +24h. |

Unique: `(license_id, route, idempotency_key)`.

## Stub mode

Build-stub of feeds-server (analogue of Build #7 for lodge-server)
ships every business route returning either:

- `501 Not Implemented` with a deterministic body, OR
- `200` with an empty list (for `GET /connections`, see above).

But the licence-token verification, edition / `feeds_enabled` gate,
audit row persistence, and idempotency replay are LIVE. This means
`RemoteBankFeedsService` in saebooks-api can be written and tested
end-to-end now, and the SISS adapter slots in behind the existing
routes when ACSISS / SISS Data Services onboarding completes.

Deterministic stub body shape:
```json
{
  "status": "stub",
  "would_have_<verb>": true,
  "stub_<noun>_id": "stub_<noun>_<uuid>",
  "comment": "feeds-server is stubbed. SISS / ACSISS Data Services onboarding pending — see bank-feeds memory."
}
```

The verb/noun pair varies per route (`would_have_initiated` /
`stub_connection_id` for create; `would_have_synced` /
`stub_sync_id` for sync; etc.). Tests in saebooks-api MUST NOT match
on the stub body strictly — they check for `status == "stub"` and
the presence of the relevant `stub_*_id` field, then move on.

## Out of scope for v1 (Build-stub)

Deliberately excluded — these unblock W4 without forcing premature
design:

- **OAuth callback / consent finalise.** SISS redirects the customer's
  browser back to saebooks (not to feeds-server). saebooks-api will
  call a future `POST /api/v1/connections/{id}/finalise` endpoint with
  the consent code; that endpoint is not in v1.
- **Webhooks from SISS.** Inbound webhook signature verification +
  delivery-idempotency. Not in v1; will be a separate route family
  `POST /api/v1/webhooks/siss/...`.
- **Cursor-paginated full-history backfills.** v1 sync is "since
  cursor"; backfill is a future Build-N concern.

These are explicit gaps so a future "real-feeds-server" build doesn't
need to break the v1 contract — it just adds new paths.

## Versioning policy

- All routes under `/api/v1/`.
- Adding new routes: backwards-compatible.
- Adding new optional response fields: backwards-compatible.
- Adding new optional request fields: backwards-compatible.
- Adding a new claim to the licence JWT (eg a new flag): backwards
  compatible — feeds-server tolerates unknown claims.
- Renaming, making a field required, or changing the gate (eg moving
  `feeds_enabled` from a claim to a server-side lookup): NOT
  backwards-compatible. Bump path major (`/api/v2/...`) and run both
  in parallel for a transition window.

## Caddy route

```
feeds.saebooks.com.au {
    import public_only_inline
    reverse_proxy 10.0.2.1:18313
}
```

Added to `/usr/local/etc/caddy/caddy.d/saebooks-license-lodge.conf`
on OPNsense (see `[[caddy-opnsense-migration]]` for the why). Reload
with `/tmp/opnsh "sh -c 'sudo service caddy onereload'"`. DNS at
Cloudflare per `[[cloudflare-saebooks-zone]]` — public+proxied A/AAAA
to OPNsense WAN; cert via DNS-01.

Bring-up of the Caddy entry, DNS record, and `docker compose up -d`
of the feeds container itself are deliberately deferred to a separate
"feeds-server bring-up" task — the v1 of this contract only requires
the repo skeleton to exist + the contract to be locked so W4 can wire
its `RemoteBankFeedsService`.

## Changelog

- **2026-05-04** — initial contract. Mirrors lodge-server (Build #7)
  shape exactly: same auth, same audit discipline, same stub-mode
  policy. Locked by Cat-C dispatch (D3) before W4 wires its remote
  client.
