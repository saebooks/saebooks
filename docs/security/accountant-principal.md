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
