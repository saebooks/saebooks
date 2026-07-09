# Testing — SAE Books

## How to run the suite

The test stack is isolated: dedicated Postgres on a private docker
network, separate `saebooks-test-api` image built from
`Dockerfile.test` (bakes pytest + tests/), pytest runs against a
freshly-migrated DB, everything torn down on exit.

```bash
cd /path/to/saebooks
bash scripts/run-tests.sh                 # full suite
bash scripts/run-tests.sh -k journal      # filter by keyword
bash scripts/run-tests.sh tests/test_invoices.py    # one file
```

Any positional args after `run-tests.sh` are passed through to
pytest via `PYTEST_ADDOPTS`. The compose file always appends
`tests/` at the end of the pytest command.

For ad-hoc runs that skip the rebuild + teardown (after the image is
already built once):

```bash
sudo docker run --rm --network saebooks-test_testnet \
  -e DATABASE_URL="postgresql+asyncpg://saebooks_test:saebooks_test_pw@db:5432/saebooks_test" \
  -e SAEBOOKS_ENV=test -e SAEBOOKS_MAIL_OUTBOX_DIR=/tmp/outbox \
  -e SAEBOOKS_TEST_TRUSTED_USER_HEADER=1 \
  -e SENTRY_DSN= -e SAEBOOKS_MAIL_HOST= -e SAEBOOKS_SQL_RO_PASSWORD=test_pw \
  saebooks-test-api bash -c \
    "alembic upgrade head >/dev/null && python -m pytest --tb=line tests/<file>"
```

**Live containers (`saebooks-<tenant>-api-1` etc.) have NO pytest.**
Never run pytest against them — the image is built without dev
dependencies.

## Suite state — 2026-05-23

Two verified snapshots, both run via `scripts/run-tests.sh`
against the isolated `saebooks-test` stack:

| Commit | Failed | Passed | Skipped | Errors | Pass rate |
|--------|-------:|-------:|--------:|-------:|----------:|
| `b788fc2` (pre-cleanup baseline) | 116 | 2201 | 8 | 48 | 93.0% |
| `70fa10f` (after 6 cleanup commits) | 79 | 2275 | 13 | 6 | 95.8% |
| `ad6ac6b` (after grpc conftest + doc fix) | 80 | 2279 | 13 | 1 | 96.6% |

(The 80-vs-79 jump between rows 2 and 3 is the grpc `test_list_excludes_revoked_by_default`
test, which was ERROR-at-setup in row 2 and now reaches the test body
and FAILs on a real assertion. Net of the grpc commit: 5 ERRORs cleared, 1 new FAIL.)

The cleanup closed **83 of 164 pre-existing reds** (165 → 81 total),
mostly by fixing fixture-level issues that cascaded into dozens of
false failures. The remainder are scattered single-file regressions
that need case-by-case judgment.

### What was fixed (post-cleanup)

| Cluster | Pre | Post | Fix |
|---------|-----|------|-----|
| A — missing default Contact in seed | ~42 ERR | 0 | autouse `seed_default_contact` fixture in `tests/conftest.py` |
| B — `ConnectDispatchMiddleware.state` missing | 13 ERR | 0 | `.state` proxy property in `saebooks/connect_app.py` |
| C — cashbook-mode bleed across test files | ~10 F | 0 | autouse teardown in `tests/api/v1/test_cashbook_e2e.py` restores seed company to `bookkeeping_mode='full'` |
| D — stale enum refs | ~17 | 0 | `READONLY/CLIENT` → `VIEWER`; `ContactType.EMPLOYEE` → `BENEFICIARY` |
| E + F — hardcoded `saebooks` DB → cross-tenant cascade | 28 (14 ERR + 14 F) | 0 | derive cross-tenant app-role engine URL from `settings.database_url` |
| G — AI extraction respx URL mismatch | 4 F | 0 | `_LITELLM_URL` from `settings.litellm_base_url` |
| H — JE delete cascade audit (real bug) | 1 F | 0 | `services/journal.delete()` now snapshots each line pre-cascade |
| J — Pydantic v2 422-body assertion | 1 F | 0 | search whole body, not just `detail` |
| pay_run_v1 enum + half-finished payroll | 7 F | 2 + 5 skip | `_ensure_employee` rewritten for Employee model; 5 service-layer-dependent tests skipped with detailed reasons |
| grpc `db_session/seeded_company/seeded_user` missing | 6 ERR | 2 + grpc conftest added | new `tests/test_grpc/conftest.py` |

### What's still red — triage backlog

**Real product bugs / data drift (top priority for review):**
- `test_reports_financial.py` — CYE balance $80,107.62 vs expected
  $86,750 ($6,642 gap, almost certainly a real posting bug)
- `test_reports_bas_cashflow.py` — 1a/1b lines computing 0.0
  instead of >= 100
- `test_fx.py` — multi-currency JE unbalanced (debits 1650 vs
  credits 1100; FX revaluation broken?)
- `test_deferred_revenue.py` — `invoice.journal_entry_id` is
  None after post (deferred revenue posting broken)

**Sign / convention drift:**
- `test_budget_vs_actual.py` — `variance_ytd` 500 vs -500
- `test_retention_pct.py` (3) — retention round-trip

**Auth / route regressions:**
- `test_companies.py` (4) — POST returning 405 Method Not Allowed
- `test_contacts.py` (1) — 422 instead of 201
- `test_hard_delete_all_routes.py` (1) — `/allocation_rules`
  returning 404 instead of 403

**Test/code mismatch (likely cosmetic):**
- `test_runtime_database_url_strict.py` (3) — `DID NOT RAISE
  RuntimeError` on prod env without `DATABASE_URL`
- `test_idempotency_race.py` (1) — unexpected
  `ClaimStatus.IN_FLIGHT`
- `test_tax_engine.py` (2) — `DID NOT WARN DeprecationWarning`
- `test_accounts_page.py` (1) — "Example Pty Ltd"
  not in rendered HTML (template branding change)
- `test_licence/test_service.py` (2) — tier detection
  `'community' == 'pro'`

**Service-layer migration in flight (do NOT touch without RS sign-off):**
- `tests/api/v1/test_pay_run_v1.py` — 5 tests skipped pending
  `services/pay_runs.add_line` reworked against Employee model
  (currently queries Contact but DB FK points at `employees`)
- `tests/services/test_payg.py` (4 FAILs) — PAYG coefficients
  flagged in memory `[[saebooks-payroll]]` as placeholder; block
  prod until ATO-XLS verified

**Plus ~25-30 scattered F's** across `test_ai_extraction`,
`test_payment_allocation_invariants`, `test_invoices`,
`test_items`, `test_tax_treatment_snapshot`,
`test_journal_delete_audit` (resurfaced after a re-run — needs
re-check), `test_je_cross_tenant_write_rles4`,
`test_admin_gate_jwt_role`, `test_payments_page`,
`test_observability`.

## Conventions

- **Default tenant id** for the test DB: `00000000-0000-0000-0000-000000000001`
- **Seed company**: pinned to `created_at = '1970-01-01'` so
  `ORDER BY created_at` always picks it first
- **Default contact**: `seed_default_contact` autouse fixture
  inserts "Pytest Default Contact" (BOTH type) on session start
- **Test bookkeeping_mode**: defaults to `"full"`. Tests that
  flip to `"cashbook"` MUST restore on teardown (see
  `test_cashbook_e2e` for the pattern)
- **Fixture scope**: most seed fixtures are `scope="session"`
  for cost; per-test mutation requires explicit teardown

## Skipped tests with reasons (current)

- `test_pay_run_v1.py::test_add_line_201`
- `test_pay_run_v1.py::test_delete_line_204`
- `test_pay_run_v1.py::test_export_aba_happy_path`
- `test_pay_run_v1.py::test_export_aba_journal_lines`
- `test_pay_run_v1.py::test_finalize_happy_path`

All blocked on `services/pay_runs.add_line` querying `Contact`
when the DB FK now points at `employees` — needs the service
reworked against the Employee model.
