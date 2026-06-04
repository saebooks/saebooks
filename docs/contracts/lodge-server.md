# saebooks-lodge-server — HTTP contract

> **Authoritative.** Consumed by `RemoteLodgementService` in
> saebooks-api (Build #8). Lock this contract before implementing
> producers or consumers.

## Deployment shape

- **Hostname:** `lodge.saebooks.com.au`
- **Runtime:** FastAPI + uvicorn behind Caddy, on r420 today.
- **Storage:** dedicated Postgres database `saebooks_lodge` on the
  shared `bosun-postgres` instance.
- **Secrets:**
  - `SAEBOOKS_PORTAL_PUBKEY` (Ed25519 pubkey, raw 32-byte base64) —
    same key as embedded in saebooks-api; lodge-server uses it to
    verify customer-supplied licence tokens.
  - `SBR_MACHINE_CRED_PFX` + `SBR_MACHINE_CRED_PASSWORD` — the ATO
    Machine Credential PFX, encrypted at rest.
  - `SBR_SSID` — software subscriber identifier (`SAE-Books`).
  - `SBR_DSP_ABN` — SAE Engineering's ABN for envelope signing.
- **Network:** outbound to ATO SBR endpoints (`https://sbr.gov.au`,
  `https://ebms.softwareauthorisations.ato.gov.au`). Inbound HTTPS
  only.

## Auth model

Every lodge-server request includes a customer licence token
(JWT signed by license-server) in the `Authorization: Bearer <token>`
header. Server:

1. Verifies signature with `SAEBOOKS_PORTAL_PUBKEY`.
2. Checks `exp` (with grace).
3. Checks `edition` is in {`pro`, `enterprise`} for STP/BAS routes
   (Pro+ feature per `services.features._PRO_FLAGS`).
4. Records `(license_id, jti, route, ts, payload_hash, ato_receipt_id)`
   in audit log.

No additional auth — the licence token IS the auth.

## Routes

### `POST /api/v1/stp/lodge`

Request:
```json
{
  "envelope_xml": "<base64-encoded SBR3 STP envelope>",
  "envelope_hash": "<sha256 of decoded envelope>",
  "submitter_abn": "12345678901",
  "payevent_id": "client-side UUID for idempotency",
  "metadata": {
    "pay_period_end": "2026-04-30",
    "employee_count": 7,
    "gross_total_cents": 423000
  }
}
```

Notes:
- `submitter_abn` — active-company ABN, sourced client-side from `CompanySettings`.
- The body field name is per-envelope; idempotency is enforced on `(license_id, payevent_id)`.

Behaviour:
1. Verify licence token, edition >= pro, has `ato_sbr` flag.
2. Verify `envelope_hash == sha256(b64decode(envelope_xml))`.
3. Sign envelope with SAE Engineering's Machine Credential.
4. POST ebMS3 to ATO SBR endpoint with SSID + ABN.
5. Persist audit row: license_id, jti, payevent_id, payload_hash,
   ato_receipt_id, ato_status, raw_response.
6. Return:
   ```json
   {
     "status": "accepted",
     "ato_receipt_id": "<from ATO>",
     "ato_timestamp": "2026-05-02T12:00:00Z",
     "warnings": [{"code": "W001", "message": "Late lodgement"}]
   }
   ```
   `warnings` is a list of `{code: str, message: str}` objects; empty list when none.

Idempotency: if the same `payevent_id + license_id` is submitted again
within 24h, return the cached prior receipt instead of double-lodging.

Status codes:
- 200 — accepted by ATO.
- 202 — queued (ATO returned a deferred receipt).
- 400 — envelope hash mismatch / malformed envelope.
- 401 — missing/invalid licence token.
- 403 — edition does not include `ato_sbr`.
- 422 — ATO rejected (validation error). Body:
  ```json
  {
    "detail": "ATO rejected the lodgement",
    "ato_errors": [
      {"code": "CMN.ATO.GEN.XML05", "message": "Invalid ABN", "field": "Payer/ABN"}
    ]
  }
  ```
  `ato_errors` is `[{code: str, message: str, field?: str}]`; `field` is omitted
  when the ATO error is not tied to a specific element.
- 502 — ATO SBR endpoint unreachable / 5xx. Client should retry with
  backoff.

#### Status / poll route — TODO (gated on PVT)

A QUEUED (202) lodgement returns a *deferred* receipt: the ATO has not yet
issued a final receipt. Resolving it later needs a status-retrieval route
(e.g. `GET /api/v1/stp/status/{payevent_id}`), backed by the ATO ebMS3
response-retrieval (SBR get-status / Pull) flow.

**This route is NOT yet contracted.** Its request/response shape and the
underlying ATO transport are deliberately left unspecified here because
they are gated on the ATO PVT (Product Verification Testing) reference
pack, which SAE Engineering does not yet hold. `RemoteLodgementService.
poll_status` raises `NotImplementedError` until this is locked; the
engine-side reconcile orchestration (`services/stp.reconcile_stp_submission`
/ `reconcile_pending_stp`) is already built and tested against a test
double, so only this transport seam is outstanding.

### `POST /api/v1/bas/lodge`

Same shape as `/stp/lodge` but for BAS envelopes. The idempotency field is
`period_id` (not `payevent_id`); idempotency enforced on `(license_id, period_id)`.

Request body differences:
```json
{
  "period_id": "client-side UUID for idempotency",
  "metadata": {
    "bas_period": "2026-Q1",
    "gst_payable_cents": 12000
  }
}
```

### `POST /api/v1/tpar/lodge`

Same shape for TPAR. The idempotency field is `year_id`; enforced on
`(license_id, year_id)`.

Request body differences:
```json
{
  "year_id": "client-side UUID for idempotency",
  "metadata": {
    "financial_year": "2025-26",
    "contractor_count": 4
  }
}
```

### `POST /api/v1/superstream/send`

Same shape for SuperStream contribution messages. The idempotency field is
`message_id`; enforced on `(license_id, message_id)`. Routed through
SAE Engineering's MessagingProvider relationship (TBD when SuperStream
work begins).

Request body differences:
```json
{
  "message_id": "client-side UUID for idempotency",
  "metadata": {
    "contribution_period_end": "2026-03-31",
    "member_count": 3
  }
}
```

### `POST /api/v1/abr/lookup`

Looks up ABR data using SAE Engineering's API quota.

Request: `{"abn": "12345678901"}`

Response (mirrors `AbrLookup` cache schema in `saebooks/services/abr/enrich.py`):
```json
{
  "abn": "87 744 586 592",
  "entity_name": "Sauer Pty Ltd ATF Saueesti Trust",
  "entity_type": "Discretionary Investment Trust",
  "gst_status": "Registered",
  "gst_effective_from": "2024-02-15",
  "abn_status": "Active",
  "abn_status_effective_from": "2024-02-15",
  "address_state": "QLD",
  "address_postcode": "4350"
}
```

`gst_status` is `"Registered"` when `gst_effective_from` is non-null, else `"Not registered"`.
`entity_type` maps to `EntityTypeName` (human-readable) from the raw ABR envelope.
`abn_status_effective_from` maps to `AbnStatusEffectiveFrom` from the raw ABR envelope.

### `GET /api/v1/audit/me`

Returns the most recent 100 audit rows for the authenticated licence.
Customer can use this to verify what's been lodged.

Row schema:
```json
{
  "id": 1234,
  "route": "/api/v1/stp/lodge",
  "payevent_id": "client-supplied UUID (field name matches the route's id field)",
  "payload_hash": "sha256hex",
  "ato_receipt_id": "<from ATO, or null if stub/pending>",
  "ato_status": "accepted",
  "ts": "2026-05-03T02:00:00Z"
}
```

`payevent_id` here is the generic label for the per-route ID field
(`payevent_id` / `period_id` / `year_id` / `message_id`). `raw_response_jsonb`
is stored server-side only and is NOT returned to the client.

### `GET /healthz`

Standard health probe.

## Stub mode

Build #7 ships all routes returning `501 Not Implemented` with a
deterministic body:

```json
{
  "status": "stub",
  "would_have_lodged": true,
  "stub_receipt_id": "stub_<uuid>",
  "comment": "lodge-server is stubbed. SBR Machine Credential onboarding pending — see ato-sbr-onboarding memory."
}
```

But the licence-token verification, edition check, and audit row
persistence are LIVE. This means `RemoteLodgementService` can be
written and tested end-to-end now, and the ebMS3 layer slots in
behind the existing route when the SBR onboarding completes.

## Database schema (initial)

- `lodgement_audit` — `(id pk, license_id, jti, route, payevent_id,
  payload_hash, ato_receipt_id, ato_status, raw_response_jsonb,
  client_ip, ts)`
- `idempotency` — `(license_id, payevent_id, ato_receipt_id, ts)`
  with `(license_id, payevent_id)` unique. The `payevent_id` column
  holds whichever per-route ID field was supplied (`payevent_id`,
  `period_id`, `year_id`, or `message_id`).

Retention: ATO requires 5-year retention for STP records. Bank-feed
audit rows retained per SISS contract terms.

## Caddy route

```
lodge.saebooks.com.au {
    reverse_proxy r420:18310
}
```

Host port `18310` (free as of 2026-05-02).

Licence-token auth is the only auth.

## Changelog

- **2026-05-03** — locked 6 ambiguities raised by Build #8 (payevent_id naming,
  submitter_abn source, warnings/ato_errors shapes, abr lookup keys, audit row schema).
