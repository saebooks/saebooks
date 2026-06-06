# Security analysis — cross-tenant "accountant" principal

**Status: REVIEW BRANCH ONLY (`feat/accountant-principal`). NOT merged, NOT deployed, NO auto-merge.**
This is the most security-sensitive surface in SAE Books: it deliberately lets one
identity cross tenant boundaries. It must not ship without Richard's human review.

Design reference: `saebooks-intercompany-accountant-design.md` §4. Isolation primitives:
`docs/db-role-split.md`, migrations 0055/0083/0085/0135, `saebooks/api/v1/deps.py`,
`saebooks/services/tenant.py`.

---

## 1. What this adds (additive only)

A **Principal**: a MYOB-style identity that may hold scoped grants to *multiple* tenants
at once (an accountant servicing several entities; a bank servicing many customer
tenants). Three new tables, one migration (`0155`), one service module, zero changes to
the existing login / tenant-resolution / RLS enforcement path for normal users.

| Table | Scope | RLS | Purpose |
|---|---|---|---|
| `principals` | global | none (no `tenant_id`) | the accountant/bank identity; optional `owned_tenant_id` for its own books; `requires_fido2` defaults true |
| `principal_fido2_credentials` | global | none | FIDO2/WebAuthn binding (FIDO2-only; no code-2FA) |
| `principal_tenant_grants` | tenant-scoped (`tenant_id`) | **FORCE RLS + `tenant_isolation`** | one tenant's scoped, revocable grant of a role to a principal — **the crux** |

Nothing in the existing schema is modified. No existing policy is relaxed. No `BYPASSRLS`
path is added.

---

## 2. Threat model

**Assets:** every tenant's accounting data, isolated today by `app.current_tenant` +
FORCE-RLS under the NOBYPASSRLS `saebooks_app` role.

**Adversaries considered:**

1. A principal granted {A, B} trying to read/write tenant C (no grant).
2. A principal trying to enumerate *another* principal's grants.
3. A malicious/compromised tenant admin trying to forge a grant that binds a principal to
   a tenant other than their own.
4. A principal trying to escalate its role within a granted tenant beyond what was granted.
5. A principal logging in without a hardware security key (code-2FA bypass).
6. A normal single-tenant user — must be **completely unaffected**.

---

## 3. Exactly where and how the tenant boundary is crossed

There is **one** mechanism, in `saebooks/services/principal.py::bind_session_to_tenant`:

1. It calls `principal_grant_role(principal_id, tenant_id)` — a `SECURITY DEFINER` SQL
   function (migration 0155) that returns the granted role iff an **active** grant exists.
2. **Only if** a role is returned does it execute `SET LOCAL app.current_tenant = '<tid>'`
   and stamp `session.info['tenant_id']`.
3. From that point the principal's session is bound to the target tenant via the *same*
   GUC a native user's request sets in `deps.get_session` — so every read/write is subject
   to the *identical* FORCE-RLS `tenant_isolation` policies a native user gets.

If no active grant exists, `NoActiveGrant` is raised **before** any binding; the GUC is
never set, so FORCE-RLS returns **zero rows** for the target tenant — exactly as it would
for a stranger. There is no second query engine, no BYPASSRLS connection, no escape hatch.
The grant table tells the principal *where it may go*; it does not itself confer data
access — data access is always re-checked by RLS at the row level.

---

## 4. The grants table — two readers, two rules (why C stays isolated)

`principal_tenant_grants` is the one table a principal may read across tenants. It is read
from two directions and the security rests on keeping them separate:

**Reader 1 — a tenant session (`app.current_tenant` = X).** Sees/writes only grants for
tenant X via the ordinary `tenant_isolation` policy
(`USING tenant_id = current_setting('app.current_tenant')`, same `WITH CHECK`). This means:
- A tenant admin manages "who can act as my books" and sees only their own tenant's grants.
- **`WITH CHECK` blocks adversary 3**: a tenant-A session cannot INSERT/UPDATE a grant whose
  `tenant_id` is B — the policy rejects it. A tenant cannot forge a grant binding a
  principal to a tenant that did not grant it. (Test:
  `test_tenant_cannot_forge_grant_for_foreign_tenant`, with positive control
  `test_tenant_can_grant_for_its_own_tenant`.)

**Reader 2 — a principal asking "which tenants can I act as?"** This is a *cross-tenant*
read that the tenant-scoped policy correctly forbids. We do **not** relax the policy.
Instead `principal_visible_grants(p_principal_id)` — a `SECURITY DEFINER` function — returns
only `status='active'` rows for the **one** principal id passed in. The service passes the
**authenticated** principal's id (server-chosen, from the verified session), never a
client-supplied value. **This defeats adversary 2**: a principal cannot pass another
principal's id to enumerate their grants, because the API never lets the client choose the
id — it is taken from the authenticated principal session. Same controlled-bypass pattern
as `webauthn_lookup_credential` (migration 0135).

**Why non-granted tenant C stays isolated (adversary 1):**
- `principal_grant_role(p, C)` returns NULL → `bind_session_to_tenant` raises, GUC unset.
- Even if an attacker bypassed the service and set `app.current_tenant = C` directly, they
  would need an active grant row for C to *do* anything meaningful — and the data they read
  in C would be C's own data, which is the point of acting-as; but they cannot set the GUC
  to C through any sanctioned path without a grant, and acting-as without a grant gives the
  principal nothing it could not get by being a stranger to C (zero rows). The grant table
  itself, queried directly under any tenant GUC ≠ C, never reveals C's grant rows.
- Proven under the NOBYPASSRLS role: `test_act_as_non_granted_tenant_denied` asserts zero
  rows for C after a denied act-as.

**Role ceiling (adversary 4):** the grant carries a `role` from the `UserRole` vocabulary;
a coherence trigger (`principal_tenant_grant_role_check`) fails closed on any unknown role
string. The granted role is the ceiling for the principal inside that tenant — downstream
role checks are identical to a native user's. (Test:
`test_invalid_role_rejected_by_coherence_trigger`.)

---

## 5. FIDO2-only (adversary 5)

`principals.requires_fido2` defaults true and there is no API to flip it off.
`assert_fido2_satisfied` refuses to mint a principal session unless at least one FIDO2
credential is enrolled — **no code-2FA fallback, ever** (standing rule). Live WebAuthn
ceremony is a documented seam (`enrol_fido2_credential`): a future
`POST /api/v1/principal/fido2/register` runs the standard attestation ceremony (reusing the
0135 `user_webauthn_credentials` machinery) and persists via this function. The persistence
half is implemented and tested now; only the ceremony wiring is deferred. (Tests:
`test_fido2_required_blocks_session_without_credential`, `test_fido2_enrolment_then_satisfied`.)

---

## 6. Normal single-tenant users are unaffected (adversary 6)

- No change to `auth.py`, `deps.py`, `services/tenant.py`, the `users` table, or any
  existing `tenant_isolation` policy.
- The three new tables are additive. `principals` / `principal_fido2_credentials` carry no
  `tenant_id` and are never named by any tenant-facing code path.
- A native user's request path is byte-for-byte identical to `main`.

---

## 7. What is tested (all under the NOBYPASSRLS `saebooks_app` role unless noted)

`tests/api/v1/test_principal_cross_tenant.py`:
- act-as A and B land reads in the bound tenant only; switch A→B isolated.
- act-as C denied (`NoActiveGrant`), zero rows for C.
- revoke grant → access gone immediately, zero rows.
- principal sees only its own active grants ({A,B}); non-granted principal sees zero and is
  denied all.
- tenant session sees only its own grant rows; tenant cannot forge a foreign-tenant grant
  (with positive control); invalid role rejected.
- FIDO2 gate blocks no-credential principals; satisfied after enrolment.

`tests/db/test_migration_0155_principal_rls.py`:
- grant table is FORCE-RLS with `tenant_isolation`; resolver functions are SECURITY
  DEFINER; `principals` is not RLS'd (intended — global, never tenant-read).

---

## 8. Open risks for Richard to review

1. **SECURITY DEFINER blast radius.** The two resolver functions run as the owner
   (BYPASSRLS). They are parameterised, filter to a single principal id, and are
   `SET search_path = pg_catalog, public` to defeat search-path hijack. The residual risk
   is that the **API layer must always pass the authenticated principal's id**, never a
   client value. The API endpoint that wires this (principal session login) is **not built
   in this branch** — only the service + DB layer. Whoever builds the endpoint must take
   the principal id from the verified session, not a path/query/body param. This is the
   single most important review point.
2. **No live WebAuthn ceremony.** The FIDO2 binding is modelled and enforced, but
   enrolment/assertion ceremony is deferred. A principal cannot actually log in until that
   seam is built. Acceptable for a review branch; must not be skipped before any real use.
3. **Grant management API not built.** Grants are created/revoked via direct SQL in tests.
   A tenant-facing "manage who can access my books" UI/endpoint is future work; when built
   it must run under the granting tenant's session so `WITH CHECK` enforces ownership.
4. **`owned_tenant_id` semantics.** A principal owning its own tenant is modelled but the
   accounting linkage (the accountant's/bank's own books reconciling to client books) is
   design-only here.
5. **Deployment ordering.** Migration 0155 adds FORCE-RLS + functions; on the live stacks
   `saebooks_app` must already exist (it does, per `db-role-split.md`). The migration
   re-asserts grants explicitly so it is safe under both the production default-privileges
   path and the test stack. No live data is touched by upgrade or downgrade.
6. **Revocation is status-based, not session-killing.** Revoking a grant stops *new*
   act-as binds immediately (verified per transaction). A principal already mid-transaction
   in a tenant when the grant is revoked completes that transaction; the next bind fails.
   Acceptable (matches how RLS GUCs are transaction-scoped), but worth noting if instant
   session kill is ever required.

---

## 9. Rollback

`alembic downgrade 0154_intercompany_phase1` drops the functions, trigger, policies, and
the three tables in FK-safe order. No data outside these tables is touched. The branch is
not deployed, so rollback is "do not merge."

---

# Part II — Login, act-as, and grant API (`feat/accountant-login`)

**Status: REVIEW BRANCH ONLY (`feat/accountant-login`, off `origin/main`). DRAFT PR, NOT
merged, NOT deployed.** Part I (above) modelled the schema + service + DB layer (migration
`0156` on main). Part II adds the three things that make the principal *usable*: the live
WebAuthn login ceremony, the "act as tenant" switch endpoint, and the tenant-side grant
management API. It is the highest-risk surface in the system — review it as such.

New code in this branch:

| File | What |
|---|---|
| `alembic/versions/0159_principal_webauthn_lookup.py` | SECURITY DEFINER `principal_webauthn_lookup_credential(bytea)` — resolve a credential by id at login (renumber at merge). |
| `saebooks/services/principal_session.py` | Mint/decode the principal JWT (`psub` + `typ="principal"`), unbound (login) + tenant-bound (act-as). |
| `saebooks/services/principal_webauthn.py` | Live FIDO2 register + authenticate ceremony, reusing the `webauthn` library (NOT the legacy `fido2_service`). The principal-id derivation lives here. |
| `saebooks/api/v1/principal_auth.py` | Router: login + register + list-tenants + act-as + bound-read; `require_principal_bearer` dep. |
| `saebooks/api/v1/principal_grants.py` | Router: tenant-side grant create/revoke/list under the granting tenant's user session. |

## 10. The login binding — `principal_id` is server-derived, full stop

This is the single most important invariant of the whole feature (foreshadowed in Part I
§8.1): **the authenticated principal id comes ONLY from the verified FIDO2 assertion — the
resolved credential's owner — and NEVER from a client-supplied parameter.**

The login ceremony (`principal_webauthn.complete_authentication`, called by
`POST /api/v1/principal/auth/webauthn/authenticate/finish`):

1. **begin** returns `PublicKeyCredentialRequestOptions` with an *empty* `allowCredentials`
   (discoverable login). The client supplies NO principal identifier at begin-time.
2. **finish** receives the assertion. We parse the credential `id` and the challenge.
3. The challenge is matched to OUR begin-call's stored challenge (anti-replay of the
   options; process-local store, 5-min TTL, separate from the user store).
4. The credential is resolved by its `id` via the SECURITY DEFINER
   `principal_webauthn_lookup_credential` (migration 0159) — no tenant/session context
   needed because `credential_id` is a 256-bit unguessable blob.
5. The assertion **signature is verified against the stored `public_key`** of that
   credential. A row match alone proves nothing; the signature is the proof of possession.
6. The anti-replay `sign_count` is bumped; the principal is confirmed active and
   FIDO2-satisfied.
7. **Only then** `principal_id` = the resolved row's `principal_id`. The endpoint mints an
   *unbound* principal token carrying that id.

There is no request field for a principal id at login. The `AuthenticateFinishRequest`
schema has exactly one field, `credential`; any extra field a client sends is ignored. A
client cannot claim "I am principal X" — it can only present a key, and the key tells us
who it is. (Tests: `test_login_derives_principal_id_from_credential_not_client` sends an
attacker `principal_id` alongside the *victim's* credential and asserts the minted session
is the **victim's**; `test_login_unknown_credential_rejected`, `test_login_bad_signature_rejected`.)

This deliberately follows `saebooks/api/v1/webauthn.py::authenticate_finish` (the proven
user passkey path) and deliberately does **not** follow
`saebooks/services/fido2_service.py`, whose `complete_authentication` is a legacy,
simplified stub that trusts a `user_id` carried in challenge state and never verifies the
signature. That stub must not be used as a template; the principal path verifies for real.

### Token shape — the user and principal surfaces are disjoint

A principal session is a JWT with `psub` (principal id) + `typ="principal"` — a *different
shape* from a user JWT (`sub` + `tenant_id`). `decode_principal_token` rejects anything
whose `typ` is not `"principal"`, so a normal user JWT can never authenticate a principal
endpoint (it has a valid signature but no `typ`). (Tests:
`test_user_jwt_is_not_a_principal_token`, `test_user_jwt_cannot_call_principal_endpoint`.)

The existing single-tenant user login/enforcement path is **byte-for-byte unchanged**: the
principal path is a parallel router with its own bearer dependency and its own token type.

> **Correction — the user router accepts a principal token (signature only); the GRANT is
> what stops it.** An earlier draft of this section claimed a principal token "confers zero
> user authority" on the user router because it carries no `sub`. That was only true for
> **admin-gated** endpoints (`/users`, hard-delete, grant-create): those need a hydrated
> `request.state.user`/role, which a `sub`-less principal token never provides, so the admin
> gate denies them. But the **non-admin data endpoints** (`/companies`, `/contacts`,
> `/invoices`, `/bills`, `/payments`, `/reports`, …) need only a *bound tenant*, not a user
> identity. A principal token is a validly-signed JWT (same secret), so `decode_access_token`
> accepts it on the user router too, and — before the fix in §11.1 — `require_bearer` stamped
> its `tenant_id` claim and `get_session` bound `app.current_tenant` to it with **no grant
> re-check**. Adversarial review confirmed two live exploits:
>
> * **A1** — a bound principal token on `GET /api/v1/companies` returned 200 with that
>   tenant's data, no grant re-check.
> * **A2 (headline)** — after the grant was **revoked**, replaying the same bound token on
>   the user router still returned 200 for the token's whole 1h TTL: revocation was not
>   immediate where the data actually lives.
>
> The fix (§11.1) enforces the grant on the **shared** auth path, so it now applies to every
> router, re-checked per request.

## 11. Act-as — the binding goes through the same FORCE-RLS as a native user

An *unbound* login token may only call `/principal/tenants` (list its own grants) and
`/principal/act-as`. `POST /principal/act-as` takes the principal id from the
**authenticated session** (not the request) and a target `tenant_id` from the body:

* it calls `resolve_grant_role` (the SECURITY DEFINER predicate) under the app role; if no
  *active* grant exists it returns `None` and the endpoint **403s — no token, no binding**;
* on success it mints a *tenant-bound* principal token (adds `tenant_id` + `role`).

A bound token's tenant queries flow through `get_principal_tenant_session`, which:

1. **re-verifies the active grant on every request** (so revocation takes effect on the
   next request, not just the next login), then
2. binds `app.current_tenant` via the **same** `session.info['tenant_id']` +
   `after_begin` listener mechanism a native user's `deps.get_session` uses.

So every read/write a bound principal performs runs under the **identical** FORCE-RLS
`tenant_isolation` policy as a native user of that tenant. There is **no BYPASSRLS data
path** — the only SECURITY DEFINER calls are the credential lookup (auth bootstrap, by an
unguessable id) and the grant resolvers (server-supplied principal id). A principal with no
grant for tenant C can never reach `app.current_tenant = C` through any sanctioned path, so
RLS returns zero rows for C exactly as for a stranger. (Tests:
`test_act_as_non_granted_tenant_denied`, `test_bound_session_isolation_under_force_rls`,
`test_revoked_grant_blocks_act_as`, `test_revoked_grant_blocks_bound_session_reuse`.)

### 11.1 The grant gate is enforced on the SHARED auth path (closes A1/A2)

`get_principal_tenant_session` re-verifies the grant only for the `/api/v1/principal/*`
router. That is **not enough**, because a bound principal token is a validly-signed JWT and
the **user** router (`/companies`, `/contacts`, `/invoices`, …) accepts the same token via
`require_bearer` (see the correction in §10). To close A1/A2 the grant is now enforced on the
**shared** dependency, so it covers **every** router and is re-checked **per request**:

`saebooks/api/v1/auth.py::require_bearer` — in the JWT branch, immediately after
`decode_access_token` succeeds and **before** `request.state.jwt_claims` is stamped — calls
`_enforce_principal_grant(request, claims)`:

1. **Detect a principal-type token.** Fires only when `typ == "principal"` **or** a `psub`
   claim is present. A normal user token (`sub` + `tenant_id`, no `typ`/`psub`) never enters
   this branch — its path is **byte-for-byte unchanged**.
2. **Unbound principal token (no `tenant_id`) → 403.** A login token has no business on a
   user data router; it may only drive `/principal/tenants` + `/principal/act-as`.
3. **Bound principal token → re-verify the live grant.** Calls `resolve_grant_role`
   (`principal_grant_role`, SECURITY DEFINER, migration 0156) on a fresh `AsyncSessionLocal()`
   — the **same** predicate `/act-as` and `get_principal_tenant_session` use, and one that is
   parameterised by `(principal, tenant)` and **independent of `app.current_tenant`**, so it
   is safe to call before any tenant GUC is bound. No **active** grant → **403, and no
   binding** (the raise happens before the claims are stamped, so `resolve_tenant_id` /
   `get_session` never bind `app.current_tenant`). Fails **closed**: any lookup error denies.

Because the check runs on the router-level `require_bearer` dependency, it executes **every
request**. So a **revoked grant takes effect immediately on the user router too** (A2 closed),
and a token bound to a tenant the principal has no grant for is rejected with zero rows (A1
closed). The bound token alone is never sufficient — the live grant is required every request.

No new `BYPASSRLS` path is introduced and the §12 admin-gate + WITH-CHECK self-grant
semantics are untouched: a principal token still hydrates **no** `request.state.user`/role
(we deliberately do not stamp them), so admin-gated endpoints keep denying it exactly as
before. The `X-Active-Tenant` switcher in `resolve_tenant_id` is also unreachable for a
principal token (it requires an ADMIN `request.state.role`, which a principal token never
sets), so a principal token can only ever bind the single tenant carried in its signed claim
— the one the grant gate just verified.

Tests (`test_principal_user_router_grant_gate.py`):
`test_bound_principal_token_with_grant_reaches_user_router` (acting-as works on the user
router), `test_a1_ungranted_tenant_bound_token_denied_on_user_router` +
`test_a1_ungranted_tenant_zero_rows_under_force_rls` (A1: 403 + zero rows),
`test_a2_revoked_grant_blocks_bound_token_on_user_router` +
`test_a2_revoked_grant_blocks_write_on_user_router` (A2: immediate deny on read **and**
write after revoke), `test_unbound_principal_token_denied_on_user_router`,
`test_normal_user_token_unaffected` (user-token path unchanged). All seven fail against the
pre-fix code and pass after.

## 12. Grant API — a tenant can only grant access to ITSELF

Grant management (`/api/v1/principal-grants`, admin-only) runs under the **granting
tenant's own user session** — ordinary `require_bearer` -> `get_session` -> `app.current_tenant`
bound from the user's JWT. That is deliberate: the **database**, not the application,
enforces ownership.

* **create** (`POST`): the grant's `tenant_id` is taken from the authenticated session
  (`resolve_tenant_id`), never from the body — the body has no `tenant_id` field at all.
  Even if it did, the `tenant_isolation` `WITH CHECK` rejects any INSERT whose `tenant_id`
  != `app.current_tenant`. **A tenant cannot forge a grant binding a principal to another
  tenant.** Role is validated app-side and the 0156 coherence trigger fails closed on an
  unknown role. (Tests: `test_tenant_admin_grants_own_tenant_then_principal_can_act_as`,
  `test_grant_tenant_id_is_session_derived_not_body`, `test_invalid_role_rejected`; the
  WITH-CHECK forge-prevention is proven in `test_principal_cross_tenant.py::test_tenant_cannot_forge_grant_for_foreign_tenant`.)
* **revoke** (`DELETE /{id}`): a soft-delete (`status='revoked'`) so the audit trail
  survives, scoped by `... WHERE id=:id AND status='active'`. Under the tenant's GUC, RLS
  confines the UPDATE to the tenant's own rows, so a foreign grant id matches zero rows ->
  404. Revoke removes access immediately for new act-as binds. (Tests:
  `test_revoke_removes_act_as`, `test_tenant_cannot_revoke_another_tenants_grant`.)
* **list** (`GET`): RLS-filtered to the tenant's own grants. (Test:
  `test_list_shows_only_own_tenant_grants`.)
* **authz**: grant management requires role >= ADMIN (reuses `_require_admin`). (Test:
  `test_non_admin_cannot_create_grant`.)

> Test-harness note: the isolation proofs that depend on FORCE-RLS run against the
> `saebooks_app` (NOBYPASSRLS) engine, because the test API process connects as the owner
> role (`DATABASE_URL=saebooks_test`) where FORCE-RLS does not isolate — the same pattern as
> `test_cross_tenant_isolation.py` / `test_principal_cross_tenant.py`. The HTTP tests prove
> the gating + wiring; the app-role tests prove the isolation guarantee.

## 13. Migration 0159

Adds only the `principal_webauthn_lookup_credential(bytea)` SECURITY DEFINER function
(`STABLE`, `search_path = pg_catalog, public`, `EXECUTE` to `saebooks_app` only). No table
is created or altered, so the new-table RLS checklist does not apply. Reversible:
`alembic downgrade 0158_reclassifications` drops the function. The number is provisional —
renumber at merge if a lower number lands first.

## 14. Open risks for Richard to review (login/grant additions)

1. **The login derivation is the crown jewel.** Re-read §10. The whole feature's safety is
   that `complete_authentication` returns an id taken from the resolved+verified credential.
   Any future refactor that lets a request field influence the principal id is a
   tenant-boundary break. The endpoint schema has no such field today.
2. **Challenge store is process-local.** Single-replica only (same limitation as the user
   WebAuthn flow). A multi-replica deployment needs Redis-backed challenges or a principal
   logging in against replica A but finishing on replica B will fail. Documented seam.
3. **First-key enrolment is out-of-band.** `register/*` requires an already-authenticated
   principal session — but a principal has no key until one is enrolled (chicken/egg). The
   first credential must be seeded by an operator (direct insert / CLI), exactly like a
   user's first key. A bootstrap CLI is future work; do not ship a self-service
   "enrol-without-a-key" path.
4. **Revocation is per-request, not session-killing.** A bound token re-checks the grant on
   every request — now on the **shared** auth path (§11.1), so this holds on the user router
   too, not just `/principal/*`. Revocation takes effect on the next request — but a request
   already in flight completes. Acceptable (matches GUC transaction scope); note if instant
   kill is ever required.
5. **Token TTL.** Principal tokens are 1h (vs 8h user tokens). Tune as needed; shorter is
   safer for a cross-tenant identity.
6. **No rate limiting on login.** The login endpoint is unauthenticated by design (it IS
   the auth). Credential ids are unguessable and the signature must verify, so brute force
   is infeasible, but a deployment should still put the usual edge rate-limit in front of
   `/principal/auth/webauthn/authenticate/*`.
7. **Audit attribution.** A bound principal acting in a tenant currently attributes writes
   via the existing audit hooks using the session's identity; wiring `psub` explicitly into
   `audit_log` for principal-originated writes is a recommended follow-up so a tenant can
   see "accountant X did this" vs a native user.
