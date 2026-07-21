# DB role split — `saebooks_app` (NOSUPERUSER + NOBYPASSRLS)

**Status:** code complete on `fix-F`; deployment is operator work, NOT yet applied to any live stack.

**Why:** Lane 4 P0-1 (audit-trail `2026-05-23-overnight/04-rls-multi-tenant.md`) — PostgreSQL silently excludes `BYPASSRLS` and `SUPERUSER` roles from `FORCE ROW LEVEL SECURITY`. Every `tenant_isolation` policy installed by migration 0055 and its follow-ups is a no-op against the running API, because the API connects as the `saebooks` superuser. The whole RLS framework is inert; tenant isolation rests entirely on the single application-layer `WHERE tenant_id = …` clause in each query. One missing filter anywhere is a cross-tenant data leak.

The fix is to split the DB into two roles:

| Role | Purpose | Attributes |
|---|---|---|
| `saebooks` (existing) | Schema owner; migrations, admin tooling | SUPERUSER + BYPASSRLS (unchanged) |
| `saebooks_app` (new) | Runtime API connections | LOGIN + NOSUPERUSER + NOBYPASSRLS |

The API container connects as `saebooks_app` for every request. Postgres' FORCE-RLS then actually fires for the `tenant_isolation` policies, giving us a second line of defence behind the app-layer filters.

## What landed on `fix-F`

### Code

1. **`alembic/versions/0056_split_db_role.py`** (already on `dev-must-fix-2026-05-24` since 2026-04-26) — creates `saebooks_app` idempotently with the right attributes; grants `SELECT/INSERT/UPDATE/DELETE` + sequence/function usage on every existing table; sets `ALTER DEFAULT PRIVILEGES` so future tables created by the migration role inherit the same grants automatically. Live-DB inspection confirms the default privileges propagated to every post-0056 tenant-scoped table.

2. **`alembic/versions/0128_app_role.py`** (this PR) — completes the unfinished business of 0056:
   - Sets the role password from the `SAEBOOKS_APP_DB_PASSWORD` migration-env var (hard-fails if unset).
   - Re-asserts NOSUPERUSER + NOBYPASSRLS + LOGIN (catches any out-of-band ALTER between 0056 and 0128).
   - Re-issues every GRANT idempotently (catches the case where 0056's `ALTER DEFAULT PRIVILEGES` was scoped to a different migration-running role than the one creating later tables — relevant in the test stack where `saebooks_test` runs migrations, not `saebooks`).
   - Verifies post-conditions: refuses to commit if `rolsuper` or `rolbypassrls` is true after the upgrade.

3. **`saebooks/db.py`** (`_runtime_database_url()`, `AppSessionLocal`, `LoginSessionLocal`) — already on `dev-must-fix-2026-05-24` from earlier work. The runtime `engine` consults `SAEBOOKS_APP_DATABASE_URL` first and only falls back to `DATABASE_URL` when the override is unset, so the same code path works in production (override set) and dev (single role; override unset). `LoginSessionLocal` is a dedicated BYPASSRLS engine reserved for pre-auth lookups (POST /auth/login etc) where `app.current_tenant` is unknown.

4. **`saebooks/api/v1/integrations.py`** — the paperless webhook handler now sets `app.current_tenant` via `SET LOCAL` before reading `paperless_webhook_secrets`. Without this, the same request that works today under the BYPASSRLS role would 404 under `saebooks_app` because the FORCE-RLS policy would filter out the matching row.

5. **`docker-compose.test.yml`** — `SAEBOOKS_APP_DB_PASSWORD` env added so the test stack's `alembic upgrade head` step can run 0128.

### Tests

- `tests/api/v1/test_integrations.py` — existing mock tests retained.
- `tests/api/v1/test_integrations_rls.py` (new) — direct asyncpg connection as `saebooks_app`, real `paperless_webhook_secrets` row, exercises the full HTTP handler. Verifies the webhook 200s under NOBYPASSRLS — which is only possible if `SET LOCAL` fired. Also includes a probe that an explicit SELECT without `SET LOCAL` correctly returns zero rows for the same tenant under FORCE-RLS — proving the policy is being enforced.

## Deployment plan — 5 live stacks

Stacks (one server compose project per tenant DB):

| # | Stack | Compose dir | DB container |
|---|---|---|---|
| 1 | sandbox | `/home/youruser/server/compose/saebooks-sandbox/` | `saebooks-sandbox-db-1` |
| 2 | primary (prod) | `/home/youruser/server/compose/saebooks-primary/` | `saebooks-primary-db-1` |
| 3 | acme | `/home/youruser/server/compose/saebooks-acme/` | `saebooks-acme-db-1` |
| 4 | app-preview | `/home/youruser/server/compose/saebooks-app-preview/` | `saebooks-app-preview-db-1` |
| 5 | cashbook-demo | `/home/youruser/server/compose/saebooks-cashbook-demo/` | `saebooks-cashbook-demo-db-1` |

**Order:** sandbox first, soak 24 h, then primary, then the other three in any order. The point of sandbox-first is that the worst case for this change is "every request 404s because RLS now actually fires but `SET LOCAL` is missing somewhere"; we want that surfacing on the stack with the lowest cost-of-failure.

**Per-stack steps:**

1. **Generate the password.** One 48-byte base64 string per stack, written to the stack's `.env`. Different password per stack so a leak on one does not compromise the others.

    ```bash
    openssl rand -base64 48
    ```

2. **Wire `SAEBOOKS_APP_DB_PASSWORD` into the API container env.** Edit `/home/youruser/server/compose/saebooks-<stack>/docker-compose.yml`, add to the `api` service `environment:` block:

    ```yaml
    SAEBOOKS_APP_DB_PASSWORD: ${SAEBOOKS_APP_DB_PASSWORD}
    SAEBOOKS_APP_DATABASE_URL: postgresql+asyncpg://saebooks_app:${SAEBOOKS_APP_DB_PASSWORD}@db:5432/<dbname>
    ```

    Add `SAEBOOKS_APP_DB_PASSWORD=<the password>` to `/home/youruser/server/compose/saebooks-<stack>/.env`.

    `DATABASE_URL` stays unchanged — it remains the owner-role URL used by alembic, the CLI, and the LoginSessionLocal engine.

3. **Apply the migration.** Restart the API container — its entrypoint runs `alembic upgrade head` which picks up 0128 and uses the env var to set the password.

    ```bash
    ssh ci-host "cd /home/youruser/server/compose/saebooks-<stack> && sudo docker compose up -d api"
    ```

4. **Verify.** Two checks on the DB container:

    ```bash
    ssh ci-host "sudo docker exec saebooks-<stack>-db-1 \
        psql -U saebooks -d <dbname> -c \
        \"SELECT rolname, rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname='saebooks_app';\""
    # expect: saebooks_app | f | f | t

    ssh ci-host "sudo docker exec saebooks-<stack>-db-1 \
        psql -U saebooks -d <dbname> -c \
        \"SELECT usename, count(*) FROM pg_stat_activity WHERE datname='<dbname>' GROUP BY usename;\""
    # expect: a row with usename='saebooks_app' (the API container's pool)
    ```

5. **Smoke-test endpoints.** Hit any tenant-scoped route on the stack and confirm 200. The `saebooks-verify` per-stack tokens (`~/bin/saebooks-verify`, saebooks-claude-verify-tokens) bypass CF Access and curl the internal API directly. Spot-check at least one endpoint per category — list, detail, mutation — because the failure mode of a missed `SET LOCAL` in a router is a zero-row response, which list endpoints surface as `{items: [], total: 0}` (looks superficially fine) but detail endpoints surface as 404 (very visible).

6. **24-h soak on sandbox before promoting** to primary. Watch `saebooks-sandbox-api-1` logs for sudden 404 spikes on previously-200ing endpoints — that is the SET-LOCAL-missing signature.

## Rollback

The change is fully reversible at the compose layer; the migration is reversible at the DB layer.

**To roll back a stack** (worst case: 404 storm because some handler is missing `SET LOCAL`):

1. Remove `SAEBOOKS_APP_DATABASE_URL` from `/home/youruser/server/compose/saebooks-<stack>/docker-compose.yml`. Leave `SAEBOOKS_APP_DB_PASSWORD` in place — harmless.
2. Restart: `ssh ci-host "cd /home/youruser/server/compose/saebooks-<stack> && sudo docker compose up -d api"`.
3. The API now falls back to `DATABASE_URL` (the BYPASSRLS owner role) and you're back to the pre-fix-F behaviour.

**To roll back the migration:**

```bash
ssh ci-host "sudo docker exec saebooks-<stack>-api-1 alembic downgrade 0127_drop_journal_tenant_default"
```

0128's downgrade nulls the password but leaves the role and its grants in place (`saebooks_app` cannot log in any more; the API falls back via the compose rollback step above). The role-drop is in 0056's downgrade; we deliberately do not drop the role in 0128 downgrade because dropping it would strand any still-running API connection.

## Manual verification — RLS now actually fires

After flipping a stack to `saebooks_app`, prove the policy is enforced by a direct DB probe:

```bash
ssh ci-host "sudo docker exec saebooks-<stack>-db-1 \
    psql 'postgresql://saebooks_app:<password>@localhost:5432/<dbname>' -c \
    \"SELECT id, tenant_id FROM paperless_webhook_secrets LIMIT 1;\""
# expect: zero rows (no app.current_tenant set → policy filters everything)

ssh ci-host "sudo docker exec saebooks-<stack>-db-1 \
    psql 'postgresql://saebooks_app:<password>@localhost:5432/<dbname>' -c \
    \"SET app.current_tenant = '<some valid tenant uuid>'; \
       SELECT id, tenant_id FROM paperless_webhook_secrets LIMIT 1;\""
# expect: 0..1 rows, all matching the GUC
```

If the first probe returns rows, FORCE-RLS is not engaged — escalate.

## CI fixture-migration plan (test suite under --rls)

Separate from the 5 live-stack deployment above: the isolated docker
test suite (`docker-compose.test.yml` / `scripts/run-tests.sh`) can now
opt into running the runtime engine as `saebooks_app` via `--rls` (or
`SAEBOOKS_TEST_RLS=1`) — see `feat/m3-ci-rls-approle`. Default CI stays
on the owner role unchanged; `--rls` is not yet wired into any CI job.

**Why opt-in, not default:** flipping the whole suite to `--rls`
reddens ~2000 tests (out of ~5000) with `git checkout feat/m3-ci-rls-approle`'s
first commit. The root cause is uniform: ~100 test files write fixture
rows directly via `AsyncSessionLocal()` without stamping the tenant GUC
(`session.info["tenant_id"]`) that `api/v1/deps.py`'s `after_begin`
listener reads to issue `SET LOCAL app.current_tenant`. Under the
owner/BYPASSRLS role this was silently fine; under `saebooks_app`,
FORCE RLS blocks the write or filters the read to zero rows.

**The fix pattern** (landed for the 19 dedicated RLS/isolation files —
`test_rls_*.py`, `test_tenant_scope.py`, `test_cross_tenant_isolation.py`,
`test_cross_company_isolation.py`, `test_principal_cross_tenant.py`,
`test_integrations_rls.py`) — two helpers in `tests/conftest.py`:

- `tenant_session(tenant_id)` — `AsyncSessionLocal()` with
  `session.info["tenant_id"]` stamped. Use for any single-tenant
  read/write in a fixture; harmless no-op under the owner role, load-
  bearing under `--rls`.
- `owner_seed_session()` — always the BYPASSRLS owner role
  (`LoginSessionLocal` / `DATABASE_URL`), for cross-tenant seed/teardown
  that legitimately writes rows into more than one tenant in a single
  fixture. Never use for the assertions under test.

Several of these files also imported `from saebooks.db import engine as
_owner_engine` to build a comparison "owner" connection or to
`ALTER ROLE saebooks_app` — that's the *runtime* engine, which IS
`saebooks_app` under `--rls`, so both uses broke. Swapped to
`saebooks.db._owner_role_engine` (the engine `LoginSessionLocal` is
built on) — always the real owner/superuser role regardless of mode.

**Remaining work — the ~100-file tail:** every other test file that
opens `AsyncSessionLocal()` directly for fixture setup/teardown needs
the same treatment (mostly `tenant_session`, occasionally
`owner_seed_session` where a fixture spans tenants). This is bulk,
mechanical, per-file work — not attempted here (task scope was the
diagnostic + a mergeable beachhead, not the full migration). Suggested
order: convert alongside normal feature work file-by-file rather than
as one giant PR, then add a `--rls` CI job once a large majority is
converted (a lingering handful of red files can stay on an ignore-list
the same way `tests/KNOWN_FAILURES.txt` works today).

**What this does NOT yet prove:** the dedicated RLS files already
validated real policy enforcement in default-mode CI (they open their
own direct `saebooks_app` connections regardless of `--rls`), so that
part of the picture is unchanged from before this branch. What's new
here is that the *application's own request path* (`AsyncSessionLocal`
via `get_session`) has been proven to work correctly against real FORCE
RLS for two files that exercise it end-to-end
(`test_cross_company_isolation.py`, `test_tenant_scope.py`). The other
~98 app-path test files have not been proven either way under
`saebooks_app` — that's exactly the gap the fixture-migration tail
above needs to close before a `--rls` CI job would mean anything
broader than "the 19 files we already trust still pass."

## Known gaps NOT closed by this work

These were called out in Lane 4 P0-2, P1, P2; they survive after fix-F:

- **P0-2** — corrupt account row + `company_id`-only filters. Requires a separate sweep adding `Model.tenant_id == tenant_id` to every router; out of scope here.
- **P1** — `_first_company_id` silently ignores `X-Company-Id` on 16 routers. Out of scope.
- **P2** — `wizard_state` policy missing `WITH CHECK`. Out of scope; once-line fix.

These all become higher-priority once fix-F lands: under the BYPASSRLS role they are latent; under NOBYPASSRLS they remain latent because they still scope (by company), but the defence-in-depth gap is the same. They are not made worse by fix-F.

## Related

- Lane 4 report — `/home/youruser/projects/saebooks-critics-reports/audit-trail/2026-05-23-overnight/04-rls-multi-tenant.md`
- Lane 5 P0-005 (paperless SET LOCAL) — same report directory.
- `new-table-rls-checklist` memory file — the checklist this work makes mean something.
- `saebooks-claude-verify-tokens` — per-stack Bearer tokens for the smoke-test step.
