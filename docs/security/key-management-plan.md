# Key Management Plan — field encryption, custody, and rotation path

**Scope:** the application-layer encryption keys protecting secrets at rest —
primarily `SAEBOOKS_FIELD_ENCRYPTION_KEY` (Fernet), plus the custody rules for
everything it protects. Named OSF item from the 2026-07-16 readiness mapping:
*"key management is a single static key in an environment variable — no KMS, no
rotation — acknowledged as version-1 scope."* This document is the version-2
plan and the interim custody rules.

---

## 1. Current state (version 1 — honest description)

* One Fernet key (AES-128-CBC + HMAC-SHA256 per the Fernet spec) in the
  `SAEBOOKS_FIELD_ENCRYPTION_KEY` environment variable on the app host.
* It protects, per column: employee TFNs, integration credentials (bank feeds),
  super-fund banking fields, and the ATO Machine Credential keystore and
  password (`saebooks/models/ato_sbr.py`).
* Fail-closed by design: with the key unset, encrypt/decrypt raise
  `FieldEncryptionNotConfiguredError` — a misconfigured install cannot silently
  persist plaintext into a column the schema promised was ciphertext
  (`saebooks/services/crypto.py`).
* No rotation has ever been performed. The crypto module docstring already
  reserves the rotation shape (key-list, first = encrypt, rest = decrypt-only).

Assessment: acceptable for Year-1 self-lodger scope (one entity, one operator,
one host). Not acceptable for commercial multi-tenant operation.

## 2. Version 2 — rotation without a KMS (pre-commercial gate)

Target: rotation becomes a routine operation before the first commercial
tenant. No cloud KMS dependency (self-hosted posture, see the residency
attestation); the mechanism is standard **MultiFernet**:

1. `SAEBOOKS_FIELD_ENCRYPTION_KEY` accepts a comma-separated key list; index 0
   encrypts, all keys decrypt (`cryptography.fernet.MultiFernet` — the exact
   future reserved in `crypto.py`'s docstring).
2. A management command (`saebooks rotate-field-keys`) walks every encrypted
   column, decrypts with the old key, re-encrypts with the new, in batched
   transactions with a resume cursor — rotation must be idempotent and
   interruptible.
3. Completion check: a scan proves no ciphertext decrypts *only* with a
   non-primary key; then the old key is dropped from the list.
4. Audit: rotation start/finish recorded as audit rows (who, when, key
   fingerprints — never key material).

Estimated engine work: small (the API surface was designed for this). It is
deliberately **not** being built speculatively today; it is gated work on the
commercial track with the design pinned here.

## 3. Version 3 — considered and deferred

A hardware or service KMS (HSM, cloud KMS, HashiCorp Vault transit) would add
key-usage audit and non-exportability. Deferred because: the self-hosted,
single-host posture makes envelope encryption with an external KMS a new
network dependency in the lodgement path; and at the 10k-record OSF threshold
the assessment posture changes anyway, which is the right moment to revisit.
Decision recorded so the OSF answer to "why no KMS?" is a reasoned position,
not an omission.

## 4. Custody rules (in force now)

* **Generation:** `Fernet.generate_key()` on the target host; never in a chat
  transcript, never in a shell history (`export` from a file, not argv).
* **Primary store:** Bitwarden (EU) is canonical for every key and password in
  this plan; the env var / env file is a deployment *mirror*, not the source of
  truth.
* **Mirrors are part of the surface:** the documented mirror of the ATO
  keystore password in the orchestrator's secrets env file must be updated (or
  retired) on every rotation — see `credential-rotation-runbook.md` §2.5.
* **Separation:** the field-encryption key and the database credentials do not
  share values with each other or with anything else (the 2026-07-07
  shared-password finding is the counterexample this rule exists to prevent).
* **Backups of keys:** the Fernet key is deliberately EXCLUDED from database
  backups (a backup that contains both ciphertext and key is plaintext with
  extra steps). Key recovery is from Bitwarden only.
* **Test keys never touch production:** the deterministic test key in
  `docker-compose.test.yml` is public-by-design and must never appear in any
  non-test environment file.

## 5. Claim → evidence ledger

| Claim | Evidence site |
|---|---|
| Fail-closed field encryption | `saebooks/services/crypto.py` |
| Encrypted columns inventory | `saebooks/models/employee.py` (TFN), `saebooks/models/ato_sbr.py` (keystore), SISS credential models |
| Rotation shape reserved in API | `crypto.py` module docstring (key-list note) |
| Deterministic test key is test-only | `docker-compose.test.yml` (marked NOT FOR PRODUCTION) |
