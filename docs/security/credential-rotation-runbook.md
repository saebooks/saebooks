# Credential Rotation Runbook — ATO Machine Credential & SBR secrets

**Scope:** the ATO Machine Credential (keystore + password), the SSID, and the
secrets adjacent to the lodgement path. Named OSF gap from the 2026-07-16
readiness mapping ("rotation is not yet documented").

**Current credential:** `ABRD:51824753556_SAE-Books`, ABN 51 824 753 556
(The Trustee for Example Trust), valid **2026-04-25 → 2028-04-24**, ATO
Production CA. Custody: Fernet-encrypted keystore + password on the
`AtoSbrConfig` row; tenants never hold it (`docs/contracts/lodge-server.md`).

---

## 1. Rotation triggers

| Trigger | Deadline |
|---|---|
| Scheduled expiry (2028-04-24) | Rotate at **T minus 60 days** (2028-02-24); calendar reminder required |
| Suspected/confirmed compromise | Immediately — this becomes an incident (`incident-response.md` §3) |
| Keystore password exposure (transcript, log, screen share) | Same day |
| Machine change of custody (new lodge-server host) | Before first production lodgement from the new host |
| RAM authorisation change (principal authority) | Review credential validity same week |

## 2. Planned rotation procedure (no compromise)

1. **Create the replacement first, revoke second** — zero-gap handover.
   In RAM (authorisationmanager.gov.au), as principal authority for
   ABN 51 824 753 556: Manage credentials → Create machine credential (requires
   the Machine Credential Downloader browser extension). Save the new
   `keystore-new.xml` + password directly into Bitwarden (EU) — never to a
   shared folder first, never into a shell command line.
2. **Stage in EVTE.** Upload the new keystore via the admin UI
   (`/admin/ato-sbr`) with the per-company environment still `evte`; run one
   AS Get/Validate against EVTE to prove the new credential signs correctly.
3. **Flip production.** Switch the config row to the new keystore; run the next
   scheduled production interaction (or a Get, which is read-only) as the
   canary.
4. **Revoke the old credential in RAM** only after the canary passes.
5. **Update records:** Bitwarden entry (old marked revoked-with-date, not
   deleted), `ato-sbr-onboarding` memory, and the mirrored
   `ATO_SBR_KEYSTORE_PASSWORD` in `~/.claude/secrets/acsiss.env` on the
   orchestrator host — that mirror is part of the credential surface and is
   easy to forget.

## 3. Compromise rotation (differences from §2)

* Order inverts: **revoke first** in RAM, accept the lodgement outage.
* Treat every secret that shared storage or transcript context with the burned
  credential as burned too (the keystore password has historically been shared
  with other infrastructure credentials — see the shared-password finding of
  2026-07-07; rotation must break that sharing, the replacement password must
  be unique).
* File the incident note and ATO notification per `incident-response.md` §3–4.

## 4. Adjacent secrets (same discipline, own cadence)

| Secret | Store | Rotation |
|---|---|---|
| SSID | `AtoSbrConfig.ssid` | Not secret in the cryptographic sense, but ATO-issued; re-issue only via the DSP product-registration channel |
| `SAEBOOKS_FIELD_ENCRYPTION_KEY` | Env on the app host | Per `key-management-plan.md` — NEVER rotate ad-hoc, ciphertexts depend on it |
| Lodge-server licence JWTs (tenant → relay auth) | License server | Revoke + re-issue per tenant; short expiry preferred over rotation ceremony |
| API tokens (`/admin/api-tokens`) | Hashed in DB | Revoke on suspicion; tokens are bearer credentials |

## 5. Standing rules

* A leaked secret is **live until explicitly revoked** — deleting the file or
  scrubbing the transcript does nothing.
* Rotation is always **revoke → re-issue → update Bitwarden → update the env
  mirrors**, in that order for compromise, reversed (issue → flip → revoke) for
  planned.
* The keystore password never appears in argv, logs, or tool output — read it
  via `bw-secret` on the orchestrator, or type it into the admin UI directly.

## 6. Claim → evidence ledger

| Claim | Evidence site |
|---|---|
| Keystore + password stored Fernet-encrypted | `saebooks/models/ato_sbr.py` (`keystore_encrypted`, `keystore_password_encrypted`) |
| EVTE default / production per-company toggle | `AtoSbrConfig.environment`; admin UI |
| Credential validity window | Keystore metadata columns (`keystore_not_before/after`); RAM record |
| Password-sharing history to break | shared-password finding 2026-07-07 (internal memory) |
