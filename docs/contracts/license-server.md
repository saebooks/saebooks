# saebooks-license-server — HTTP contract

> **Authoritative.** Consumed by saebooks-api (`LicenseService.refresh()`,
> `/api/v1/license/refresh`), by saebooks-web (Settings → Licence
> "refresh now" button), by the marketing-site Stripe Checkout flow,
> and by Stripe webhooks. Lock this contract before implementing
> producers or consumers.

## Deployment shape

- **Hostname:** `license.saebooks.com.au`
- **Runtime:** FastAPI + uvicorn behind Caddy, on r420 today.
- **Storage:** dedicated Postgres database `saebooks_license` on the
  shared `bosun-postgres` instance (own schema + RLS).
- **Secrets:** `SAEBOOKS_LICENSE_SIGNING_PRIVKEY_B64` (Ed25519 priv key,
  raw 32-byte base64), `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `LICENSE_SMTP_*`. All env-driven; nothing committed.
- **Auth model:**
  - `/api/v1/license/refresh` — unauthenticated; identity is proven by
    presenting an existing valid token signed by us.
  - `/api/v1/license/issue-trial` — unauthenticated; rate-limited per
    IP and per email; sends token to the verified email.
  - `/stripe/webhook` — Stripe signature verified.
  - `/admin/*` (later) — admin-group session JWT.

## Token shape (signed by license-server, verified by saebooks-api)

JWT with `header.alg = "EdDSA"`. Payload:

```json
{
  "license_id":  "lic_<ulid>",
  "ledger_id":   "led_<ulid>",
  "customer_id": "cus_<stripe>",
  "edition":     "business",
  "licensed_to": "Acme Pty Ltd",
  "iat":         1714600000,
  "exp":         1717278400,
  "grace_until": 1717883200,
  "jti":         "jti_<uuid>",

  "seat_admin_cap":    2,
  "seat_employee_cap": 3,
  "company_cap":       2
}
```

- `edition` is the canonical knob (community/offline/business/pro/
  enterprise). `seat_*_cap` and `company_cap` are optional overrides on
  the per-edition default in `services.licence.caps`.
- `exp` carries the end of the current Stripe billing period.
- `grace_until` extends `exp` by SAE Engineering's grace policy
  (currently `exp + 7d`); customer can keep operating up to this
  point. After `grace_until`, the binary drops to community per
  CHARTER §6.6.

## Routes

### `POST /api/v1/license/refresh`

Refresh an existing token against current Stripe state.

Request:
```json
{ "current_token": "<JWT>" }
```

Behaviour:
1. Verify signature on `current_token` with our public key. Reject
   if invalid (HTTP 400 `{"error":"invalid_token"}`).
2. Look up `license_id` in the local DB.
3. Read Stripe subscription status:
   - `active` / `trialing` → mint new token at the current edition;
     `exp = current_period_end`, `grace_until = exp + 7d`.
   - `past_due` → mint same edition, `exp = now + 24h` (short-cycle
     for retry), `grace_until = exp + 14d`.
   - `canceled` / `unpaid` → mint downgraded token: `edition="community"`,
     `exp = now + 30d`, `grace_until = exp + 30d` (give them time to
     export).
4. Persist refresh event (license_id, ip, ua, jti_old, jti_new, ts).
5. Return:
   ```json
   {
     "token": "<JWT>",
     "edition": "business",
     "expires_at": "2026-06-02T00:00:00Z",
     "grace_until": "2026-06-09T00:00:00Z"
   }
   ```

Error responses:
- 400 `{"error":"invalid_token"}` — signature/decode failure.
- 404 `{"error":"unknown_license"}` — token verified but not in DB
  (tampered / revoked).
- 429 `{"error":"too_many_refreshes"}` — > 100 refreshes/24h for the
  same `license_id` (abuse signal).

### `POST /api/v1/license/issue-trial`

Mint a 14-day Business-edition trial without payment.

Request:
```json
{
  "email": "owner@acme.com",
  "licensed_to": "Acme Pty Ltd",
  "ledger_id": "led_<ulid>"
}
```

Behaviour:
1. Rate-limit: 1 trial per IP per 24h, 1 trial per email lifetime.
2. Persist trial row in DB.
3. Mint token with `edition="business"`, 14-day `exp`.
4. Email token to `email` via SMTP.
5. Return `{"status":"sent"}` regardless of whether email already
   exhausted its quota (avoid email-enumeration leak).

### `POST /stripe/webhook`

Stripe → license-server event consumer.

Verify `Stripe-Signature` header against `STRIPE_WEBHOOK_SECRET`.
Handle:

- `checkout.session.completed` — locate or create license row, mint
  initial token, email to customer.
- `customer.subscription.updated` — update local sub state. No token
  push (client refreshes on its own schedule).
- `customer.subscription.deleted` — mark `canceled` in DB. Next
  `/refresh` returns the downgrade token.
- `invoice.payment_failed` — mark `past_due` in DB.

Always return 200 to Stripe even on internal error (so Stripe doesn't
retry-storm); log + alert internally.

### `GET /healthz`

Returns `{"status":"ok","service":"license-server","version":"<git_sha>"}`.

## Database schema (initial)

Tables under schema `license`:

- `license` — `(id pk, ledger_id, customer_id, email, licensed_to,
  edition, seat_admin_cap, seat_employee_cap, company_cap,
  created_at, current_jti)`
- `subscription` — `(id pk, license_id fk, stripe_subscription_id,
  status, current_period_end, last_event_id, updated_at)`
- `refresh_event` — `(id pk, license_id fk, ip, ua, jti_old, jti_new,
  ts)`
- `trial` — `(id pk, license_id fk, email, ip, issued_at)`
- `webhook_event` — `(id pk, stripe_event_id unique, type, payload jsonb,
  received_at, processed_at)`

All tables get a `tenant_id` column even though license-server is
single-tenant — discipline §4.1 from the infrastructure plan applies
to commercial servers too.

## Caddy route

```
license.saebooks.com.au {
    reverse_proxy r420:18309
}
```

Host port `18309` (free as of 2026-05-02 — `18305`/`18306` are taken by
headscale/paperless-gpt).

Terminates TLS at OPNsense Caddy (post-migration) or r420 Caddy
(today). `/api/v1/*` and `/stripe/webhook` are unauthenticated
(license-token / Stripe-signature verified). `/admin/*` (later) is
gated by an admin-group session JWT.

## Versioning

- Path-prefixed: `/api/v1/`. Breaking changes go to `/api/v2/`.
- Token format additions: backwards-compatible JSON additions only.
  Renames or required-claim additions = bump major and embed both
  old + new pubkey in saebooks-api during transition.
