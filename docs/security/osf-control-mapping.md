# OSF Control Mapping — SAE Books

Working map of the ATO DSP **Operational Security Framework** control areas to
SAE Books' position. Source of the assessment: the 2026-07-16 ATO DPO meeting
prep (Amrik Singh, ticket DSPPT-49560). The OSF is the **production-access
gate** — not required for DSP registration or EVTE build/test (confirmed by
the ATO service desk, 2026-07-16) — and the gate before any third-party tenant
lodges through SAE Books.

Scale context: OSF categories are risk-driven, keyed on who controls the
product and the ~10,000 unique-client-record threshold. SAE Books Year 1
(one entity, self-lodged) sits in the low-volume self-assessment category.

| OSF control area | SAE Books position | Status |
|---|---|---|
| Authentication / MFA | WebAuthn/FIDO2 passkeys built and tested; myID (strong) used for ATO onboarding; FIDO2-mandatory cross-tenant principal model designed (`docs/security/accountant-principal.md`) | Built (principal model pending) |
| Encryption in transit | HTTPS-only on all boundaries incl. lodge-server and ATO SBR | Built |
| Encryption at rest | Fernet field encryption (TFNs, credentials, MC keystore+password); hard-fail on missing key | Built; rotation path: `key-management-plan.md` |
| Multi-tenant segregation | FORCE RLS on tenant tables; NOBYPASSRLS app role; cross-tenant regression tests | Built; role rollout completing across stacks |
| Credential management | Machine Credential isolated server-side; tenants hold licence JWT only | Built; rotation: `credential-rotation-runbook.md` |
| Audit logging (OSF min: 12-month immutable) | Lodgement audit rows, 5-year retention; DB triggers block UPDATE/DELETE | Built; event coverage extending |
| Security monitoring | Transaction-layer via audit trail | Partial; infra monitoring not formalised |
| Incident response / breach notification | **`incident-response.md`** (this directory) | Documented 2026-07-16 |
| Personnel security | Sole operator; RBAC in-product; docs due before first hire / Year 2 | N/A at current scale |
| Data hosting / residency | **`hosting-residency-attestation.md`** — self-hosted Brisbane, onshore | Documented 2026-07-16 |
| Independent assessment / pen test | None yet; Year-1 volume ≪ 10k records → self-assessment category | Expected at this stage |
| Change control / environments | EVTE default; production via explicit per-entity toggle | Built; EVTE runs blocked on SSID |
| AI / data handling | OCR-extraction only, human-reviewed drafts, no ATO data in model path, no training | Built (architectural boundary) |

## Commercial (stage 2) open items, in order

1. **Restricted database role rollout** across all running stacks (technical, in progress).
2. **Audit event coverage** across all financial event types (technical, in progress).
3. **Field-key rotation command** (version 2 of `key-management-plan.md`) — pre-commercial gate.
4. **Client-passphrase backup-export claim** — verify implementation site before any OSF submission (`hosting-residency-attestation.md` §3).
5. **OSF self-assessment questionnaire** — submit when production access is sought; the four documents in this directory are its inputs.
6. **DSP re-engagement for multi-tenant** — current SSID is single, self-lodger, EVTE-only; the commercial model needs the DSP team's sign-off and possibly a different SSID arrangement.

References: OSF requirements — softwaredevelopers.ato.gov.au/operational_framework
(v6.05 lineage; verify current version at submission time).
