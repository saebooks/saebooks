# Hosting & Data Residency Attestation

**Purpose:** the OSF expects onshore-by-default hosting and asks the DSP to
attest to where taxation, payroll, and superannuation data lives. The facts
below were true operationally before this document existed; this page makes
them attestable. Named OSF gap from the 2026-07-16 readiness mapping.

**Attested by:** Richard Sauer, Director, Example Pty Ltd.
**Review cadence:** re-attest on any production re-homing, and at each OSF
questionnaire submission.

---

## 1. Production locality

* The SAE Books production instance (application, PostgreSQL, and the
  lodge-server boundary) runs on **SAE Engineering's own hardware in Brisbane,
  Queensland, Australia** — owner-operated, not colocated with a third party.
* **No taxation, payroll, accounting, or superannuation data is stored or
  processed offshore.** Backups replicate to a second owner-operated host on
  the same Australian premises network (see §3).
* Remote administration occurs over an authenticated private overlay network;
  administrative access does not move data offshore. (The operator may
  administer while travelling; the data does not travel.)

## 2. Sub-processors and boundaries

| Boundary | Data crossing it | Residency note |
|---|---|---|
| ATO SBR (EVTE/production) | Lodgement documents, signed | ATO endpoints, Australia |
| Stripe (where a tenant enables invoicing payments) | Invoice/payment metadata for that feature | Stripe's own PCI environment; no TFNs, no ledger export |
| AI extraction (document inbox OCR) | User-supplied source documents only | Anthropic API via gateway; **no ATO data, no TFNs in the model path; no training** (architectural boundary, see `saebooks/services/ai_extraction.py`) |
| Email (transactional) | Message content as sent | No tax identifiers in transactional mail templates |

Anything not in this table stays on the Brisbane hosts. Adding a sub-processor
that touches tax/payroll data requires updating this attestation **before**
go-live.

## 3. Backup locality & protection

* Scheduled backups run from a systemd timer (`scripts/backup.sh` lineage; see
  `saebooks/services/backups.py` module docstring for the design rationale) to
  storage on a second owner-operated Australian host.
* Backup media do not leave Australia. No cloud object storage is currently in
  the backup path; if that changes, the region must be Australian and this
  document updated first.
* The OSF briefing (2026-07-16) additionally describes client-passphrase
  encrypted export backups (scrypt + AES-256-GCM, passphrase discarded by the
  system). ⚠ That implementation is **not in the engine repo** — verify the
  implementation site and confirm the claim before quoting it in an OSF
  submission.

## 4. Multi-tenant posture (Year 2+)

The residency story does not change with commercial tenants: tenant data lands
in the same Australian-resident PostgreSQL with FORCE row-level security. If
commercial scale forces a move to hosted infrastructure, the constraint is
locked in advance: **Australian region, residency-guaranteed tier, attestation
updated before migration** — residency is a design input, not an afterthought.

## 5. Claim → evidence ledger

| Claim | Evidence site |
|---|---|
| Self-hosted, owner-operated, Brisbane | Infrastructure records (storage dependency map, internal); physical custody by attester |
| Tenant segregation on the same host | FORCE RLS migrations + `tests/api/v1/test_cross_tenant_isolation.py` |
| AI path excludes ATO data / TFNs | `saebooks/services/ai_extraction.py` |
| Backup design | `saebooks/services/backups.py`, `scripts/backup.sh`, systemd timer |
| Client-passphrase export encryption | ⚠ UNVERIFIED in-repo — locate before external use |
