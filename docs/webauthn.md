# WebAuthn / FIDO2 — native passkey login

SAE Books ships with built-in WebAuthn / FIDO2 support. Users can enrol
hardware security keys (YubiKey, etc.), platform authenticators
(Touch ID, Windows Hello), or passkeys against their account, then sign
in by tapping the key — no password, no third-party identity provider
required.

## Architecture

Two layers:

* **API** (`saebooks`) — `/api/v1/auth/webauthn/*` endpoints that
  generate registration / authentication options, verify attestations
  and assertions, and store credentials in `user_webauthn_credentials`.
  Uses [py-webauthn](https://pypi.org/project/webauthn/) for the
  cryptographic plumbing.
* **Web** (`saebooks-web`) — server-side proxies under `/auth/webauthn/*`
  that forward JSON to the API, plus the HTML pages that drive
  `navigator.credentials.{create,get}` in the browser. Mints the
  saebooks-web session cookie from the JWT returned by
  `authenticate/finish`.

The credential storage table (`user_webauthn_credentials`) is tenant-
scoped with FORCE ROW LEVEL SECURITY. The login path uses a
`SECURITY DEFINER` function `webauthn_lookup_credential(bytea)` to
resolve a credential to (user, tenant) without a prior session — the
only legitimate path that needs to look at credentials across tenants.

## Per-instance configuration

WebAuthn credentials are bound to the relying-party (RP) identifier
which **must match the public hostname** of the instance. So each
saebooks deployment has its own credential set. To enable:

```env
SAEBOOKS_WEBAUTHN_ENABLED=1
SAEBOOKS_WEBAUTHN_RP_ID=books.example.com         # MUST be the public hostname
SAEBOOKS_WEBAUTHN_RP_NAME=My SAE Books            # display name in the prompt
SAEBOOKS_WEBAUTHN_ORIGIN=https://books.example.com # exact origin (with scheme)
```

For multi-origin setups (dev + prod, or canary hostname alongside
production), `SAEBOOKS_WEBAUTHN_ORIGIN` accepts a comma-separated list.

Without `SAEBOOKS_WEBAUTHN_RP_ID` / `_ORIGIN` set, the endpoints return
`503 webauthn_not_configured`.

## Endpoints

API (`saebooks`):

| Method | Path                                              | Auth      | Purpose |
|--------|---------------------------------------------------|-----------|---------|
| POST   | `/api/v1/auth/webauthn/register/begin`            | required  | Returns `PublicKeyCredentialCreationOptions`. |
| POST   | `/api/v1/auth/webauthn/register/finish`           | required  | Verifies attestation, stores credential. |
| POST   | `/api/v1/auth/webauthn/authenticate/begin`        | none      | Returns `PublicKeyCredentialRequestOptions` for passkey login. |
| POST   | `/api/v1/auth/webauthn/authenticate/finish`       | none      | Verifies assertion, returns a JWT. |
| GET    | `/api/v1/auth/webauthn/credentials`               | required  | List the current user's credentials. |
| DELETE | `/api/v1/auth/webauthn/credentials/{id}`          | required  | Delete a credential. |

Web (`saebooks-web`):

| Method | Path                                              | Notes |
|--------|---------------------------------------------------|-------|
| GET    | `/auth/webauthn/login`                            | Passkey landing page. |
| GET    | `/settings/security`                              | Enrollment + management UI. |
| `*`    | `/auth/webauthn/{register,authenticate,credentials}/*` | Server-side proxy to the API. The browser only talks to saebooks-web. |

## Flow — registration

```
browser                         saebooks-web                  saebooks-api
  |                                  |                              |
  | POST /auth/webauthn/register/begin                              |
  |--------------------------------->|                              |
  |                                  | POST /api/v1/.../register/begin
  |                                  |   (Bearer = user's JWT)      |
  |                                  |----------------------------->|
  |                                  |          publicKey {...} <---|
  |       publicKey {...} <----------|                              |
  | navigator.credentials.create()   |                              |
  | (user taps key / Touch ID)       |                              |
  | POST /auth/webauthn/register/finish                              |
  |   {credential, friendly_name}    |                              |
  |--------------------------------->|                              |
  |                                  | POST /.../register/finish    |
  |                                  |----------------------------->|
  |                                  |        verifies attestation, |
  |                                  |        INSERTs row, returns  |
  |                                  |        {credential_id, name} |
  |                                  | <----------------------------|
  |          200 OK <----------------|                              |
```

## Flow — authentication (passkey login, no prior session)

```
browser                         saebooks-web                  saebooks-api
  |                                  |                              |
  | POST /auth/webauthn/authenticate/begin                          |
  |--------------------------------->|----------------------------->|
  |                                  |             publicKey {...}  |
  |                                  | <----------------------------|
  |     publicKey {...} <------------|                              |
  | navigator.credentials.get()      |                              |
  | (user taps key — discoverable)   |                              |
  | POST /auth/webauthn/authenticate/finish                         |
  |   {credential}                   |                              |
  |--------------------------------->|----------------------------->|
  |                                  |   SECURITY DEFINER lookup by |
  |                                  |   credential_id (no tenant), |
  |                                  |   then verify_assertion(),   |
  |                                  |   bump sign_count, mint JWT  |
  |                                  | <----------------------------|
  |                                  | session_cookie set,          |
  |                                  | {redirect: "/"} returned     |
  |  200 + cookie <------------------|                              |
  | window.location = "/"            |                              |
```

## Security properties

* **No password.** Credentials are public-key pairs; we store only the
  public key. A leaked database can't be used to impersonate anyone.
* **Anti-replay.** Each credential has a `sign_count` that's bumped on
  every successful authentication. Assertions with `<= stored` count
  are rejected.
* **RP-bound.** Credentials registered for one saebooks instance are
  unusable on a different one (different RP id). Lost-key compromise is
  contained per-instance.
* **Tenant isolation.** Storage table has FORCE RLS + tenant_isolation
  policy. The cross-tenant lookup path uses a `SECURITY DEFINER`
  function that returns ONLY what's needed for assertion verification
  (user_id, tenant_id, public_key, sign_count). The session that's
  minted is for the credential's owner.
* **Optimistic discoverable-credential preference.** Registration
  options request `residentKey: preferred`, so platform authenticators
  store the credential ID on the key itself — login is a single tap
  with no username typed in. Hardware keys without resident-key support
  still work; they're just not username-less.

## Operator notes

* **Apply migration 0135** on every instance you want to enable
  WebAuthn on. It adds the `user_webauthn_credentials` table, RLS
  policy, tenant-coherence trigger, the SECURITY DEFINER lookup
  function, and a sync trigger maintaining the legacy
  `users.fido2_credential_count` column.
* **Challenge store is in-memory** (single-process, 5min TTL). For
  multi-replica deployments swap `_ChallengeStore` in
  `saebooks/api/v1/webauthn.py` for a Redis-backed implementation.
* **Password reset still works.** WebAuthn co-exists with the existing
  email + password and OAuth/OIDC paths; you can mix and match per user.
* **No external IdP needed.** Customers self-hosting SAE Books don't
  need Authentik, Cloudflare Access, or a third-party SSO to get
  hardware-key login. RP_ID + ORIGIN env vars are the only setup.
