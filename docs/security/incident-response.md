# Incident Response & Breach Notification Procedure

**Scope:** SAE Books production (self-hosted, Brisbane) and the ATO SBR lodgement
path. Written for the current operating scale — a sole operator — and staged for
multi-tenant operation. This is the named pre-Year-2 OSF deliverable from the
2026-07-16 ATO DPO readiness mapping.

**Owner:** Richard Sauer, Director, Example Pty Ltd (sole
operator; every role below is held by the owner until the first hire, at which
point this document must be revised with separation of duties).

---

## 1. What counts as an incident

Severity is set by the most sensitive data class plausibly touched:

| Class | Examples | Severity |
|---|---|---|
| Lodgement credentials | ATO Machine Credential keystore or password, SSID misuse | **Critical** |
| Tax identifiers | TFNs (employees/contacts), ATO correspondence | **Critical** |
| Tenant financial data | Ledger, banking details, payroll rows crossing a tenant boundary | **High** |
| Integration secrets | Bank-feed credentials, API tokens, Stripe keys | **High** |
| Availability | Prolonged outage of the books or lodgement service, ransomware | **Medium–High** |
| Attempted, unsuccessful | Blocked probes, failed auth storms | **Low** (log + monitor) |

A **notifiable data breach** in the OAIC sense is unauthorised access to or
disclosure of personal information (or loss where access is likely) that a
reasonable person would conclude is likely to result in serious harm. TFNs are
explicitly high-harm identifiers.

## 2. Detection surfaces

* Immutable lodgement audit rows (hash, receipt, status — UPDATE/DELETE blocked
  by DB triggers) — divergence between engine records and ATO-side receipts is
  a primary tamper signal.
* Cross-tenant isolation: FORCE row-level security plus the cross-tenant
  regression suite (`tests/api/v1/test_cross_tenant_isolation.py`). Any RLS
  bypass observed in production is automatically Critical.
* Application logs and auth failures (WebAuthn ceremony errors, token misuse).
* Infrastructure monitoring on the host stack (out of repo scope; see
  `docs/security/hosting-residency-attestation.md`).

## 3. Response procedure

Times are targets from the moment the operator becomes aware.

**T+0 — Triage (immediately).**
Classify per §1. Open an incident note (timestamped, append-only — keep it in
`~/records/` on the orchestrator host, not in the affected system). Preserve
evidence before changing state: snapshot containers/volumes, copy logs off-host.

**T+1h — Contain.**
By class:
* *Machine Credential compromise:* revoke the machine credential in **RAM**
  (Relationship Authorisation Manager, authorisationmanager.gov.au) → generate a
  replacement → re-upload via the admin UI. Treat the old keystore password as
  burned. Notify the ATO (see §4) — credential compromise is always reportable
  to the ATO regardless of OAIC thresholds.
* *TFN / tenant data exposure:* identify the access path and close it (disable
  the route/token/account); rotate `SAEBOOKS_FIELD_ENCRYPTION_KEY` **only** per
  the key-management plan (naive rotation strands ciphertexts).
* *Integration secrets:* revoke at the provider first, then re-issue; secrets
  are live until explicitly revoked, deletion is not revocation.
* *Availability/ransomware:* isolate the host from the network before touching
  disks; restore is from off-host backups, never in place.

**T+24h — Assess.**
Establish: what data, which tenants/individuals, over what window, exfiltrated
or exposed-only. The immutable audit rows and DB trigger design mean the
forensic substrate survives an application-level compromise.

**T+30 days (hard ceiling) — Notify.**
* **OAIC:** if the assessment concludes an eligible data breach is likely, notify
  the OAIC and affected individuals as soon as practicable. The Privacy Act
  allows at most **30 calendar days** for the assessment itself — do not use all
  of it if the conclusion is already clear.
* **ATO:** report security incidents affecting the SBR channel, the Machine
  Credential, or taxation data to the DSP contact channel (ticket
  DSPPT-49560 lineage / Digital Partnership Office, DPO@ato.gov.au) without
  delay — verify the current wording of the DSP terms of use for the exact
  obligation before relying on this paragraph.
* **Tenants (Year 2+):** contractual notification per the subscription terms;
  individuals notified directly where the OAIC scheme requires it.

**Post-incident (within 2 weeks).**
Written post-mortem in `docs/security/incidents/` (create on first use):
timeline, root cause, controls that worked/failed, remediation list with dates.
Update this procedure with what was learned.

## 4. Contact register

| Party | Channel | When |
|---|---|---|
| ATO Digital Partnership Office | DPO@ato.gov.au; DSP service-desk ticket | Machine Credential / SBR channel / tax-data incidents |
| OAIC | oaic.gov.au NDB form | Eligible data breaches |
| RAM | authorisationmanager.gov.au | Credential revoke/reissue |
| Bank / Stripe / feed providers | Provider dashboards | Integration-secret compromise |

## 5. Claim → evidence ledger

| Claim in this document | Evidence site |
|---|---|
| Immutable audit rows, UPDATE/DELETE blocked | DB triggers (migrations 0125/0161/0190 lineage); lodgement audit tables |
| Cross-tenant isolation enforced + tested | FORCE RLS migrations; `tests/api/v1/test_cross_tenant_isolation.py` |
| Field encryption hard-fails when unconfigured | `saebooks/services/crypto.py` (`FieldEncryptionNotConfiguredError`) |
| Credential custody split (tenants never hold the MC) | `docs/contracts/lodge-server.md` |
| 30-day OAIC assessment ceiling | Privacy Act 1988 Part IIIC (verify current text when citing externally) |
