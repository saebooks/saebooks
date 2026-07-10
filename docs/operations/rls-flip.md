# RLS role-flip plumbing for `saebooks.cli`

Status: **built but not enabled** (branch `feat/rls-cli-app-role`, 2026-05-03).

## What's in this commit

| Piece                                | Where                                                           |
|--------------------------------------|-----------------------------------------------------------------|
| SECURITY DEFINER enumerator          | `alembic/versions/0084_bank_feeds_secdef_enum.py`         |
| Strict app-role engine (`AppSessionLocal`) | `saebooks/db.py`                                          |
| CLI per-tenant `SET LOCAL app.current_tenant` + bypass-role guard | `saebooks/cli.py` (`sync-feeds`, `refresh-feed-issues`) |
| Unit + RLS-isolation tests           | `tests/services/bank_feeds/test_rls_cli.py`, `tests/test_cli.py` |
| Cron template (commented)            | `deploy/cron-sync-feeds.template`                                |

## Why the CLI needs its own engine

Migration 0056 split the DB role: `saebooks` is BYPASSRLS (used for
DDL and the legacy web app), `saebooks_app` is NOBYPASSRLS (the
target runtime role). RLS is `FORCE`d on every customer-data table
by 0055, so `saebooks_app` only sees rows where the
`tenant_isolation` predicate returns true:

```
(tenant_id = (current_setting('app.current_tenant', true))::uuid)
```

A cron walker that does `SELECT * FROM bank_feed_accounts` under
`saebooks_app` with no GUC set returns **zero rows**. To enumerate
across tenants without sneaking back to a BYPASSRLS role, the CLI
calls a SECURITY DEFINER function whose body runs as the function
owner (`saebooks`, BYPASSRLS=t). The function returns the
`(company_id, tenant_id, account_id)` triple for every active feed;
the CLI then opens one transaction per `(company, tenant)` group,
runs `SET LOCAL app.current_tenant = <tid>`, and calls the existing
per-company sync logic. RLS does the rest.

## Why `SET LOCAL` not `SET`

`SET LOCAL` is bound to the current transaction. After commit, the
GUC reverts. That matters because asyncpg's pool reuses connections:
`SET` would leak the last tenant id to the next caller and turn the
RLS predicate into a cross-tenant time-bomb. `SET LOCAL` is the
SQLAlchemy `after_begin`-listener pattern used by the request-scoped
web sessions — the CLI uses the same idiom for the same reason.

## Activation procedure

The flip is one env var + one migration once the upstream guard
(see the `audit-trail/06`-style note in
`/home/youruser/server/compose/saebooks/.env`) is resolved:

1. Set `SAEBOOKS_APP_DATABASE_URL` in
   `/home/youruser/server/compose/saebooks/.env`. The password lives at
   `/home/youruser/.claude/secrets/saebooks-app-db.env` (already stashed
   as part of this branch).
2. `ssh ci-host 'cd /home/youruser/server/compose/saebooks && sudo docker compose exec -T api alembic upgrade head'` — applies migration 0084.
3. Smoke-test: `python -m saebooks.cli sync-feeds`. Should log
   `connected as DB role=saebooks_app`. Production has zero active
   feeds today so the run is a quick no-op.
4. Install the cron template at `/etc/cron.d/saebooks-feedsync` (see
   `deploy/cron-sync-feeds.template`).

## Why the WEB app didn't flip in this commit

The web app's `DATABASE_URL` keeps using the BYPASSRLS owner role
until the 25 unscoped routers documented in the compose `.env` are
audited. The CLI is a separate code path with no router surface, so
it can move first. The audit is tracked in `audit-trail/`-prefixed
notes alongside the existing P0 cross-tenant leak diagnosis.

## Reverting

* Remove `SAEBOOKS_APP_DATABASE_URL` from compose `.env` and restart
  the api container — the CLI will refuse to start (exit 2) with a
  clear error, which is the desired loud failure.
* `alembic downgrade -1` drops the SECDEF function. The runtime
  state of `bank_feed_accounts` is untouched.

## What's NOT in this commit

* No prod env file is modified.
* No cron entry is installed.
* No web router DB-role change.
* The migration is **not** applied — `alembic check` recognises 0084
  as a pending head; `alembic upgrade head` is the operator step.
