# Multi-jurisdiction engine — v0.1.4 schema scaffold

This is the schema and connection layout for the multi-jurisdiction
engine introduced at v0.1.4. It does **not** ship rate values for
super, PAYG, brackets, etc. — those follow in later releases via the
reference-bot pipeline. The goal of v0.1.4 is to land the database
shape and the seed-loader machinery so subsequent releases are pure
data updates.

## Two databases, one cluster

| Database                    | Role             | What lives there                                              |
| --------------------------- | ---------------- | ------------------------------------------------------------- |
| `saebooks_company_<uuid>`   | per company      | The customer's books — accounts, invoices, journals, etc.     |
| `saebooks_reference`        | shared, read-only at runtime | Jurisdiction master data: rates, brackets, codes, calendars   |

Both databases live in the same Postgres cluster. Two SQLAlchemy
engines connect with two different roles:

- `reference_app` — runtime API role; sets `default_transaction_read_only=on`
  via connect_args. Reads only.
- `reference_owner` — alembic + seed loader role; full DDL + write.

There are **no foreign keys across databases.** When the company DB
needs to validate that a `tax_periods.jurisdiction` value resolves to
a known jurisdiction, the service layer queries the reference DB
through `ReferenceSession` and asserts the row exists. Same pattern
for `tax_codes` (the company-DB code references a reference-DB
master).

The reasoning is operational, not theoretical:

1. The reference DB ships as a single artefact. A licensee can drop
   in an updated rate set without running migrations on every
   tenant's company DB.
2. The reference DB is short-lived schema-wise — it can be dropped
   and recreated from migrations + seeds at any time.
3. A bug in reference seeding does not have to risk corrupting the
   company DB; the boundary makes the blast radius bounded.

## Environment variables

| Variable                              | Used by                | Notes                                            |
| ------------------------------------- | ---------------------- | ------------------------------------------------ |
| `DATABASE_URL`                        | company-DB engine      | Existing variable; unchanged.                    |
| `SAEBOOKS_APP_DATABASE_URL`           | company-DB app role    | Existing variable; unchanged.                    |
| `REFERENCE_DATABASE_URL`              | reference runtime engine | Empty = reference engine disabled (dev convenience). |
| `REFERENCE_MIGRATION_DATABASE_URL`    | alembic_reference + seed loader | Owner role; required for migrations + seeds.     |

If `REFERENCE_DATABASE_URL` is unset the API still boots and
`ReferenceSession` is `None`. Code paths that require reference data
should raise `ReferenceNotConfiguredError` rather than silently
falling back. This keeps the absence loud.

## Migrations

```text
alembic/                       — company-DB migrations (existing tree)
  versions/
    0100_multi_jurisdiction_company.py   ← v0.1.4 additions

alembic_reference/             — NEW reference-DB migrations
  env.py
  versions/
    0001_initial_reference_schema.py     ← v0.1.4 first migration
```

Numbering for company migrations restarts at 0100 to leave a buffer
between the v0.1.3 quotes work (latest tracked: 0097) and the v0.1.4
multi-jurisdiction wave.

Apply the company-DB migration the usual way:

```bash
docker compose exec api alembic upgrade head
```

Apply the reference-DB migration with the dedicated config:

```bash
REFERENCE_MIGRATION_DATABASE_URL=postgresql+asyncpg://reference_owner:...@db:5432/saebooks_reference \
  docker compose exec api alembic -c alembic_reference.ini upgrade head
```

The `alembic_reference/env.py` refuses to run if the env var is
unset. This is deliberate: pointing the reference migrations at the
company DB by accident would be very bad.

## Seed loader

```bash
# Load every jurisdiction's seed data:
python -m saebooks.cli reference-load --all

# Or one jurisdiction:
python -m saebooks.cli reference-load AU

# Optionally stamp schema_meta with a version tag:
python -m saebooks.cli reference-load --all --version-tag 2026-05-09-base
```

The loader is idempotent: every row is upserted by its declared
natural key. Re-running on already-loaded data is a no-op.

The loader expects YAML files at:

```text
saebooks/seeds/jurisdictions/_global/{jurisdictions,currencies,countries}.yaml
saebooks/seeds/jurisdictions/<JUR>/<table>.yaml
```

Schema:

```yaml
table: tax_codes
key:   [jurisdiction, code, effective_from]
rows:
  - jurisdiction: AUS
    code: GST
    name: GST on sales (10%)
    rate_percent: 10.0000
    direction: sale
    effective_from: 2000-07-01
  - ...
```

The natural-key list under `key:` becomes the `ON CONFLICT` target.
Loader rejects rows with unknown columns (typo guard) and complains
loudly if the table name is unknown.

## Adding a new jurisdiction

The PR pattern (also pitched in marketing as "the next jurisdiction
is a pull request"):

1. Add the registry row to `_global/jurisdictions.yaml`.
2. Create `saebooks/seeds/jurisdictions/<CODE>/`.
3. Add at minimum:
   - `fiscal_year_definitions.yaml`
   - `tax_codes.yaml`
   - `chart_template.yaml`
4. Run `python -m saebooks.cli reference-load <CODE>` and verify
   row counts.
5. From v0.1.5 onwards, also add `tax_return_box_definitions.yaml`
   and `tax_rules.yaml` for the return forms you want to generate.
6. Add a Strategy class at `saebooks/jurisdictions/<code>_strategy.py`
   that knows the protocol envelope and periodisation arithmetic
   (this lands from v0.1.5).

No license-server entitlement required. The full multi-jurisdiction
engine ships in the AGPL community tier.

## M1.5 — global reference completeness (2026-07)

Driven by an internal reference-data completeness audit (2026-07):
the v0.1.4 scaffold was jurisdiction-parameterised but the *concepts* through it were
Australia-shaped, and several tax families plus the whole business-structure dimension
could not be represented. M1.5 generalises the concepts the **engine** must be able to
store and resolve for any jurisdiction (the engine is the gatekeeper; services on top can be
wrong). Every change is additive and non-breaking — existing AU rows keep working and become
seed data over a generic schema.

Landed themes (see the audit report for the full 129-gap register):

- **T3 — hierarchical jurisdiction.** `jurisdictions` gains `parent_code` (self-FK),
  `level` (`country|state|province|county|city`) and `iso_subdivision_code`. A country can now
  own sub-national tax jurisdictions — US federal+state+local, CA federal GST + provincial
  PST/HST, sub-national VAT, state stamp duty. (ref migration `0002`)
- **T4 — legal-entity / business structure.** New reference table `entity_structure_types`
  (`RefEntityStructureType`): per-jurisdiction local structure names mapped to a canonical
  `canonical_bucket` (`sole_trader|partnership|company_limited|pass_through|trust|pension_fund|
  nonprofit|cooperative|government|other`). Company DB gains nullable `companies.entity_structure_code`
  (service-validated against the company's jurisdiction, no cross-DB FK). Pty Ltd / trust / SMSF /
  LLC / C-corp / LLP / pension plan are now representable. (ref `0003`, company `0177`)
- **T1 — canonical tax family.** `RefTaxCode` and company `TaxCode` gain `tax_family`
  (`vat_gst|us_sales_use|excise|customs_duty|withholding|other`) + `input_credit_recoverable`,
  alongside the legacy free-text `tax_system`. GST / VAT / TVA / IVA now resolve to ONE family
  (`vat_gst`); the credit flag is what actually separates VAT/GST from US sales-&-use tax.
  (ref `0005`, company `0179`)
- **T2 — normalised per-line tax components.** New company table `journal_line_tax_components`
  (1:many, tenant-scoped + RLS): co-existing taxes on one line are first-class queryable rows
  (India CGST+SGST, US state+county+city, excise-then-VAT, reverse-charge pairs) instead of the
  single `gst_amount` scalar + `tax_treatment` JSONB blob. Populated at the single central
  snapshot point `services.journal._apply_tax_treatment` (one component per line today; the AU
  engine returns a single treatment — the schema is ready for engines that return several).
  (company `0180`)
- **T7 — canonical payroll reference tables.** New reference tables `withholding_tables`
  (wage/dividend/interest/royalty/non-resident withholding, formula params in JSONB) and
  `social_contribution_schemes` (employee/employer social insurance, rate/cap/mechanism),
  alongside the AU `payg_withholding_scale` / `medicare_levy` (rename deferred). (ref `0004`)
- **T10 — canonical bank routing.** New company table `bank_routing_identifiers` (tenant-scoped
  + RLS + coherence trigger): polymorphic owner (account/contact/employee/super_fund) × routing
  scheme (`au_bsb|iban|swift_bic|us_aba_routing|uk_sort_code|sepa|other`) + optional BIC. Existing
  `bsb`/`apca_user_id` columns retained (additive). (company `0178`)
- **T6 — generic retirement vehicle + mandatory contribution.** AU super is modelled
  (`super_fund`) but AU-only. New reference tables `retirement_vehicle_types`
  (`RefRetirementVehicleType`: per-jurisdiction local vehicle name → canonical `canonical_bucket`
  (`occupational_pension|personal_pension|self_directed|state_pension|defined_benefit|
  defined_contribution|other`) + `tax_treatment` (`EET|TEE|ETT|other`)) and
  `mandatory_contribution_rules` (jurisdiction, payer (`employer|employee|both`), rate, earnings
  base, optional age-band JSONB + cap). Seeded for AU (APRA fund → occupational_pension/EET, SMSF
  → self_directed/EET, Superannuation Guarantee 11.5% employer on ordinary time earnings). The
  enum values are shaped to also cover US 401(k)/IRA, UK workplace pensions and CA RRSP — only AU
  is seeded so far. `super_fund`/`employee` are untouched; generalising `super_fund` itself onto
  this layer is a deferred, coordinated pass. (ref `0006`)

**Test harness note:** the test image now copies `alembic_reference*` and the test stack migrates
the reference DB + sets `REFERENCE_MIGRATION_DATABASE_URL` (derived from `DATABASE_URL`), so the
reference-DB tests run in CI instead of silently skipping.

Still to come in M1.5 (from the audit): T8 (data-drive the return calculator off
`tax_return_box_definitions` instead of hard-coded ATO BAS), T5 (duties as a postable event), T9
delta (canonical tax-identifier gaps over the existing `business_identifiers`), income/CGT, and
the coordinated `super_fund` → generic `retirement_accounts` rename (T6 follow-up).

## Out of scope at v0.1.4

These follow in later releases:

- Real rate values for `super_*`, `payg_*`, `income_tax_brackets`,
  `medicare_levy`, `fbt_rates`, `ato_interest_rates`, `payroll_tax_rates`,
  `stamp_duty_rates`, `fuel_tax_credit_rates`. Reference-bot loads
  these from regulator-published sources.
- Strategy classes (`AuStrategy`, `EeStrategy`, `NzStrategy`,
  `UkMtdStrategy`).
- `lodge-server` integration. `lodgement_records` is the receipts
  table; the wire call is in the private commercial relay.
- Actual return generation (turning `figures` JSONB into BAS / KMD /
  GST101 / VAT100).
- Web UI surface (`/tax/periods`, `/tax/returns`).
- License-server gate. Reminder: the engine is community-tier; only
  transmission to the regulator is commercial.

See the plan in the library (doc id `58d74752-70e0-48c8-8346-f357251356a5`)
for the implementation order across v0.1.5 → v0.1.9.
