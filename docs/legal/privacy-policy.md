# SAE Books — Privacy Policy

> **DRAFT v0.1 (2026-07-16) — NOT YET IN FORCE.** Prepared for review by an
> Australian solicitor before publication. Structured against the Australian
> Privacy Principles; facts align with
> `docs/security/hosting-residency-attestation.md` (keep the two in sync).

**Who we are.** Example Pty Ltd (ABN 51 824 753 556),
trading as **SAE Books**. Contact for privacy matters: [accounts@example.com].

**Scope.** This policy covers personal information we handle when you use the
SAE Books hosted service or interact with us. Your business's *customers'*
personal information inside your books is handled by us as your service
provider, on your instructions; you remain responsible for your own privacy
obligations to those individuals.

## 1. What we collect

* **Account information** — name, email, business details (ABN,
  registrations), authentication data (passkey public keys — we never hold
  your private key; hashed API tokens).
* **The records you keep in the software** — contacts, invoices, bills,
  banking records, payroll records (which include employee names, addresses,
  dates of birth, pay, superannuation details), and documents you upload.
* **Tax file numbers** — only where you use payroll or lodgement features.
  See §4; TFNs get their own rules.
* **Technical data** — authentication and audit logs, IP addresses, service
  telemetry needed to run and secure the service.

We collect information directly from you and from the people you authorise.
We do not buy data about you.

## 2. Why we use it (APP 6)

To provide and secure the service; to compute and prepare documents you
direct (payroll, activity statements, lodgements); billing; support;
meeting our legal obligations (including tax record-keeping and the ATO
digital service provider requirements); and service improvement using
aggregated, de-identified data only. **We do not sell personal information
and we do not use your records to train machine-learning models.**

## 3. Disclosure (APP 6, APP 8)

We disclose personal information only to:

| Recipient | What | Why |
|---|---|---|
| Australian Taxation Office | lodgement documents you authorise (which include TFNs where the form requires them) | you direct the lodgement |
| Superannuation funds / clearing systems | member and contribution details | payroll functions you use |
| Stripe | billing name, email, payment method | subscription payments |
| Document-extraction AI processing | the source documents you upload for extraction (receipts, supplier invoices) — **never TFNs, never ATO correspondence** | the document-inbox feature; extraction output is a draft you review |
| Our infrastructure and email providers | operational data incidental to hosting and transactional mail | running the service |

**Overseas disclosure:** production data — including all financial records,
payroll data, and TFNs — is stored and processed **in Australia** (Brisbane).
Payment processing and document-extraction requests may involve overseas
processing by the providers named above, limited to the data classes shown.

## 4. Tax file numbers

TFNs are handled under the *Privacy (Tax File Number) Rule 2015*:
collected only for lawful payroll/lodgement purposes; stored encrypted at
the field level; visible only where the function requires it; included only
in documents lawfully requiring them (ATO lodgements, super contributions);
never used to link records for any other purpose; and deleted or
de-identified when no longer required by law. A TFN is never sent to any
AI or analytics system.

## 5. Security (APP 11)

Field-level encryption for high-sensitivity data (TFNs, credentials,
banking fields); encryption in transit everywhere; database-enforced tenant
isolation; passkey (FIDO2) authentication; tamper-evident audit logging; and
documented incident-response and key-management procedures (see
`docs/security/`). No security is absolute; §7 covers breaches.

## 6. Access, correction, retention (APP 12, APP 13)

You can access and correct your account information in the product, and
export your records at any time. For anything you cannot self-serve, contact
us; we respond within a reasonable time and do not charge for access
requests. We retain records while your subscription is active and for the
period after termination described in the Terms of Service, except where
tax law requires longer (payroll/lodgement records: five years).

## 7. Data breaches

We maintain a breach-response procedure. If a data breach is likely to
result in serious harm, we will notify affected individuals and the Office
of the Australian Information Commissioner in accordance with the Notifiable
Data Breaches scheme, and the ATO where the breach touches the lodgement
channel.

## 8. Complaints

Contact [accounts@example.com] first — we will acknowledge promptly and
respond within [30] days. If unresolved, you may complain to the OAIC
(oaic.gov.au).

## 9. Changes

We will post changes here and, for material changes, notify account holders
by email with at least [30] days' notice.

**[Solicitor review checklist: employee-records exemption interplay for
tenants; whether the AI-extraction disclosure needs named-provider and
region specificity; cookies/analytics statement once the marketing site
adds any; APP 5 collection-notice wording at signup.]**
