# Payday Super Phase 1 — build blockers / skipped items

Branch: `feat/payday-super-phase1`
Build date: 2026-05-30 (autonomous build, Saturday window)

Filed in-repo because the sandbox for this build session was scoped to
`/home/youruser/projects/saebooks` only; the brief's
`/home/youruser/agent-findings/` path was not writable.

## Skipped: payslip template super-contributions section

**Brief deliverable #5 — payslip template update.**

The spec asked for a "Super contributions" section after the YTD block:

    | Fund | Member # | Amount | Lodged on |

There is **no payslip HTML template** in the `saebooks` repo. The
canonical payslip template lives at
`saebooks-web/templates/payslips/single.html` (referenced from
`saebooks/services/payslip.py:4`), which is a separate repository the
build constraint explicitly forbids editing:

> Stay inside the listed file paths. Do not touch saebooks-web …

`saebooks/services/payslip.py` is pure data assembly — it returns a
dict. It already includes a `super` block (fund_name, usi, member_number,
amount, is_smsf) but does NOT carry the lodgement metadata (lodgement
run id, submitted_at, submitted_reference) that the new
"Lodged on" column would require.

### Recommended Phase 2 action

1. Extend `build_payslip()` to accept an optional
   `super_lodgement: dict | None` argument and surface it under
   `super.lodgement` in the returned dict.
2. Update the saebooks-web `single.html` payslip template to render the
   new column when present.
3. Add a service helper `lookup_super_lodgement_for_payslip(pay_run_id,
   employee_id)` to populate it from `super_lodgement_runs` ⋈
   `super_lodgement_lines`.

Doing this now would either (a) require touching saebooks-web (out of
scope) or (b) leave dead fields in the payslip dict with no consumer.

## SAFF v1 column coverage caveats

The `lines_to_saff_csv` implementation in
`saebooks/services/super_stream.py` is **bookkeeper-facing v1**, sized
to the manual portal-upload workflow. The ATO SAFF spec carries ~140
columns; this Phase 1 output emits the subset the clearing-house portal
demands for upload. Every column we leave blank in Phase 1 is tagged
`# TODO` in `_SAFF_COLUMNS`. Items to revisit in Phase 2:

- Employer ABN / name (need a join through `companies` from the run-level
  service helper).
- TFN / SMSF bank fields (encrypted at rest; operator confirmation gate
  required before plaintext export).
- Employee demographic fields not on the model: `dob` (we have it),
  `title`, `gender`, `suffix` (none modelled).
- APRA fund ABN — separate ABR/USI mapping table, not present.
- Member voluntary / spouse / child contribution amounts — Phase 1
  reports the consolidated `pay_run_line.super_amount` as `sg_amount`;
  the SS / additional split lives in `payg_breakdown` jsonb and will
  need a categorisation pass.

Before wiring the SAFF generator to clearing-house API submission
(Phase 2), re-validate the column ordering against the live ATO
schema.
