# SAE Books — Licensing Specification

> *Your books. Your database. Your control.*
>
> This document is customer-facing. It explains how SAE Books licensing
> works end to end — what each edition gives you, how licences are
> delivered and validated, what happens if your USB is lost or your
> subscription lapses, and what your appeal rights are. If anything
> here conflicts with the licensing agreement (EULA / ToS), the
> licensing agreement is the legally binding document; this spec
> describes how we *implement* it.
>
> Subordinate to `CHARTER.md §6`. Commercial numbers live in
> `SPEC-PRICING.md` (private).

---

## 1. What you get, by edition

SAE Books ships as a single codebase that runs in one of five editions,
selected at runtime by the licence in effect. Upgrading to a higher
edition is always additive — no feature you already use is removed by
an upgrade.

### 1.1 Community (free, AGPL-3.0)

- Complete single-company bookkeeping: CoA, journals, sales, purchases,
  contacts, banking, reconcile, fixed assets v1, GST/BAS report
  generation, full data export.
- Immutable ledger audit mode.
- 1 admin user, 1 company, Postgres or SQLite backend.
- AGPLv3 licence — you may modify and redistribute under AGPL. No
  commercial restrictions.
- No licence key required. The app runs out of the box.

### 1.2 Offline (perpetual, USB-bound)

- Everything in Community.
- Multi-currency + FX revaluation.
- Inventory (weighted-average cost).
- Projects + budgets.
- Fixed assets v2: diminishing-value depreciation, partial disposal,
  CSV bulk import, tax-vs-book depreciation split.
- Multi-company runtime (1 company included; Offline is for customers
  who want the feature code present but operate a single entity).
- Open Journal + Hybrid audit modes.
- Granular permissions matrix (45 codes × custom roles).
- All themes we ship (default, MYOB Classic, and any others).
- BAS report generation for AU (prep only).
- 1 admin user.
- **Not included:** bank feeds, ABR / LEI / Companies House lookup,
  ATO SBR e-lodgement, SAE-hosted SMTP. These are subscription-tier
  features because they cost us money per customer per month.

### 1.3 Business (subscription)

- Everything in Offline.
- Bank feeds (SISS / ACSISS daily sync).
- ABR lookup (AU business number enrichment on contacts).
- Stripe + Paperless integrations (SAE-hosted keys or bring your own).
- SAE-hosted SMTP for invoice / statement email delivery.
- 2 admin seats + 3 employee seats included; paid add-on seats
  available.
- Up to 2 companies under one subscription.

### 1.4 Pro (subscription)

- Everything in Business.
- LEI / GLEIF lookup.
- UK Companies House lookup.
- ATO SBR e-lodgement (BAS, and STP when offered).
- QuickBooks Online data import (migration tooling).
- Ad-hoc SQL query tool.
- Audit snapshot service.
- Automated scheduled backups.
- 5 admin seats + 10 employee seats included; paid add-on seats
  available.
- Up to 3 companies under one subscription.

### 1.5 Enterprise (subscription)

- Everything in Pro.
- Per-company SISS credentials (separate bank-feed contracts per
  entity).
- Unlimited admin + employee seats.
- Unlimited companies under one subscription.
- Priority support with a defined response-time SLA.
- Signed releases + LTS branches.
- Custom integrations and bespoke reporting.
- Hosted SaaS option with the same feature set.

---

## 2. Two licensing paths

Offline uses a **perpetual licence** model. Business / Pro / Enterprise
use a **subscription licence** model. Community uses no licence at all.

| Dimension | Perpetual (Offline) | Subscription (Business / Pro / Enterprise) |
|---|---|---|
| Payment | Once off (payment plans available) | Monthly or annual |
| Ownership | You own the licence forever | Active while you pay |
| Binding | USB hardware device | Ledger (company legal entity) |
| Activation | One-time online handshake | Portal login + Stripe / invoice |
| Network calls after activation | None required | Weekly check-in by default; grace period during outages |
| Updates | 12 months included; optional maintenance plan | Included while subscription active |
| If lapsed / USB lost | See §5 (replacement / recovery) | See §6 (grace period / read-only) |
| Can be transferred | Yes, free, up to 2× per 12mo | Yes, via portal |

---

## 3. Licence validation — how it works

Licence validation is **local, offline, and cryptographic**. There is
no phone-home licence check in the traditional DRM sense.

### 3.1 Community

No check. The app runs. Community is AGPL; validation would be
meaningless.

### 3.2 Offline

1. Licence file lives on a **USB drive** you own.
2. Drive has an immutable hardware identifier (GUID).
3. On activation, SAE Books generates an Ed25519 keypair **on the USB
   itself**. The private key never leaves the drive.
4. Our portal signs a licence file that includes: licence ID, edition,
   your company legal name, the USB's GUID, the USB's public key,
   activation date, and `updates_until` date.
5. The licence file is written to the USB.
6. At app startup (and every 24h while running), SAE Books:
   - Verifies the licence file's signature against a public key
     bundled in the binary.
   - Verifies the USB GUID on the drive matches the one in the
     licence.
   - Challenges the USB to sign a random nonce with its private key
     and verifies against the public key in the licence.

All three checks run offline. After initial activation, your system
never needs an internet connection for licence validation.

### 3.3 Subscription

1. Licence is a JWT issued by our portal, signed with our master key.
2. JWT payload includes: subscription ID, edition, list of ledger IDs
   covered, seat caps, expiry date, grace period.
3. Installed in SAE Books via `saebooks licence install <jwt>` or the
   admin UI.
4. SAE Books verifies the JWT signature locally against the bundled
   public key.
5. Weekly background check-in with the portal refreshes the JWT and
   picks up any seat / company changes.
6. If the portal is unreachable, the app runs on the last-known JWT
   until its grace period expires.

---

## 4. The USB model (Offline) — in detail

Offline is SAE Books' direct answer to customers who remember paying
once for software that they actually owned. The model takes direct
inspiration from UnRAID: the licence is bound to a hardware device you
own, not to our servers.

### 4.1 What you need

- Any USB flash drive with a stable hardware identifier (vendor ID,
  product ID, and serial — virtually every drive made since 2010
  qualifies).
- A one-time internet connection to complete activation.
- A computer you control, running SAE Books' self-hosted container.

### 4.2 Activation flow

1. Purchase Offline on the SAE Books portal. You receive an activation
   code by email.
2. Plug in your USB drive and run:
   ```
   saebooks licence activate <activation-code> --usb /dev/sdXY
   ```
3. The command:
   a. Generates an Ed25519 keypair on the drive.
   b. POSTs your activation code + USB GUID + USB public key to our
      portal.
   c. Receives a signed licence file and writes it to the drive.
4. The portal marks your activation code consumed. The USB is now
   your licence carrier.
5. SAE Books is now running Offline. Point it at the USB in settings
   (`SAEBOOKS_LICENCE_USB=/dev/sdXY` or the `/admin/licence` screen).

### 4.3 Daily operation

- The USB must be plugged in and readable at app startup.
- The app re-verifies every 24 hours. If the USB is removed, the app
  enters a 7-day grace period (fully functional, but shows a warning
  banner). After 7 days without the USB, the app drops to
  **read-only + export** mode — you can still access and export your
  data, but writes are blocked until the USB returns.

### 4.4 Backups — the USB is not a single point of failure for your books

Your **books** live in Postgres, not on the USB. The USB is only a
licence carrier. If the USB dies:

- Your books are fine. Export still works.
- You lose only the *licence*, not the data.
- You can request a replacement (see §5).

---

## 5. Transfers, replacements, and blacklisting (Offline)

### 5.1 Transferring the licence to a new USB

You may transfer your Offline licence to a new USB drive up to
**2 times per 12-month rolling window**, free of charge. This covers:

- Replacing a drive that's wearing out.
- Migrating to a faster drive.
- Changing machines where the USB doesn't fit the new form factor.

Transfer flow:

1. On the portal, log into your account and click "Transfer USB."
2. Plug the new USB into the machine running SAE Books and run:
   ```
   saebooks licence transfer --from /dev/sdXY --to /dev/sdZZ
   ```
3. The command generates a new keypair on the new drive, gets a new
   signed licence file from the portal, and revokes the old licence
   in the portal's records.
4. The old USB's GUID is added to the portal's **blacklist**.

### 5.2 Blacklist semantics

A USB GUID on the blacklist is a revoked licence carrier. If SAE Books
ever sees a blacklisted GUID again, it's treated as a potential
licence violation.

**First blacklist event (your own transfer): no consequences.** You're
just retiring an old drive.

**Reuse of a blacklisted GUID** (e.g. the old USB turning up on another
customer's system): triggers a licensing-violation flag on both the
original account and the reusing account. Both account holders receive
an email describing the incident and asking for an explanation.

- **30-day rectification period.** If the reuse was accidental (e.g.
  the old drive was sold without being wiped), acknowledge and
  deactivate the reused licence within 30 days. No further action
  taken.
- **Unresolved at 30 days.** The currently active USB on the account
  is also blacklisted. The account's Offline licence enters
  read-only + export mode.
- **Repeat offenders.** SAE Books reserves the right to terminate the
  licence under the AUP (see §8).

### 5.3 Appeals

Every blacklist decision is appealable to a human reviewer. Open an
appeal via the portal's "Dispute this action" button, or email
`licensing@saebooks.com.au`. We respond within 14 days. SAE Books does
not maintain secret or automated-only termination processes.

### 5.4 Lost USB

If you lose your USB drive and cannot transfer from it:

1. Log into the portal and report the loss.
2. The old GUID is blacklisted immediately.
3. One free replacement per 12-month rolling window is granted
   automatically.
4. Subsequent replacements in the same window incur a modest fee (see
   `SPEC-PRICING.md`), chargeable because abuse of "lost USB" would
   otherwise defeat the transfer limit.

---

## 6. Subscription licences

### 6.1 Binding

Subscription licences are bound to your **ledger identifier** — a
stable hash of your company legal name and ABN. One subscription can
cover multiple ledgers (see §6.3 on merging).

### 6.2 Renewal

Subscriptions renew at the end of each billing period. Failed payment
triggers:

- Day 0: Failure notification.
- Day 1–14: App continues running normally. Daily reminder emails.
- Day 15–30: App enters a "grace period" — fully functional, warning
  banner visible.
- Day 31+: App drops to **read-only + export** mode. Your data is
  never deleted; you can always export it.
- Day 60+: Subscription cancelled. Data remains accessible in read-only
  mode for 12 months. After 12 months, if still uncancelled, your
  account is closed and your data is deleted 90 days after that
  (you'll receive multiple warnings before any deletion).

### 6.3 Mergers, splits, and other corporate actions

If your business structure changes (acquisition, sale of subsidiary,
restructure), the portal supports:

- **Merging two subscriptions** into one — for when one customer
  acquires another that's already on SAE Books. Both ledgers roll into
  the surviving subscription, which auto-bumps to the smallest tier
  that fits the combined seat + company count. Proration credit
  applied.
- **Splitting one subscription** — for when you sell a subsidiary.
  The departing ledger gets a new subscription (at current pricing).
  The original subscription auto-downgrades if the remaining ledger
  count dropped below the current tier's minimum.
- **Ledger detach** — for winding down a company. The ledger is
  detached from the subscription; data remains readable for 12 months
  and exportable indefinitely.

All three flows are self-service through the portal. Automated checks
flag unusual patterns (e.g. many merges/splits in a short window) for
human review. In every case you have the right to appeal the portal's
decision under §8.

### 6.4 Converting a subscription to Offline

You may convert a subscription to an Offline perpetual licence at any
time, subject to the subscription tier you're leaving:

- Converting covers only the features available in Offline — paid-API
  integrations (bank feeds, ABR, LEI, CH, ATO SBR) do not transfer.
- Your data is unaffected. Postgres keeps running; only the licence
  changes.
- You pay the published Offline price (minus any prorated credit
  we agree on case by case).
- A fresh USB-activation flow applies.

---

## 7. Updates — opt-in, explicit

The SAE Books update policy is a direct consequence of the charter:
**your books, your control** includes when and whether to update.

### 7.1 By default, nothing auto-updates

- Container images don't pull new versions unless you configure them
  to.
- No "update available" banners are shown unless you enable them.
- No background update checks run without your consent.

### 7.2 Explicit update UI

The `/admin/updates` screen shows:

- Your current version.
- The latest version (fetched only when you open the screen).
- The full changelog since your current version.
- A single explicit "update now" button.

Nothing runs without that click.

### 7.3 Your install keeps running, forever

A customer who never updates still has a working accounting system on
the version they bought. We do not hold functioning code hostage
behind a "your version is too old, upgrade or lose access" flow.

- Offline licences have a documented `updates_until` date
  (activation + 12 months by default, extended by optional maintenance
  renewals). Past that date, the app continues to run — only
  new-version pulls are gated.
- Subscription licences receive updates continuously while active. If
  the subscription lapses, the app continues to run on the last
  version that was current at lapse.

### 7.4 Security updates

We will issue clearly-flagged security updates separately from feature
updates. Security updates are made available to **every edition,
including expired Offline maintenance and lapsed subscriptions**, for
the current major version. Holding a security fix hostage to a
subscription would violate the hero promise.

---

## 8. Acceptable Use Policy (summary)

The full AUP and termination procedure live in the EULA / ToS. In
summary:

- The licence is not available to entities on international sanctions
  lists, to organisations materially involved in armed conflict
  targeting civilians, to organisations whose primary purpose is
  documented human-rights violations, or to recognised hate groups.
- SAE Books reserves the right to terminate commercial licences for
  cause under the AUP.
- Every termination is appealable to a human reviewer with a 14-day
  SLA.
- Data export rights persist for 30 days post-termination regardless
  of cause. The hero promise does not die at the licensing gate.
- Community AGPL rights are not subject to the AUP. AGPL cannot be
  revoked by an acceptable-use clause; the AUP applies to commercial
  licences only.

---

## 9. Self-compiling — AGPL rights

SAE Books is AGPLv3. You have the right to download, modify, and
redistribute the source. That includes the right to flip feature flags
and run a self-compiled build with features enabled that would require
a commercial licence in a pre-built binary.

- Self-compiling to flip flags is **not a licence violation**.
- Running an unflagged *commercial-licensed* binary (i.e. our signed
  release with a valid licence key) with a tampered licence or a
  revoked USB *is* a licence violation.
- Practically, the friction of rebuilding, self-supporting, foregoing
  signed releases and LTS, and handling updates yourself is the reason
  most customers buy a licence. The moat is convenience and support,
  not DRM.

If you run a self-compiled build in production, you're on your own for
updates, security patches, and support — but you get to keep your
freedoms. That's the AGPL deal.

---

## 10. Data portability — always

Regardless of edition, licence status, or termination:

- Your Postgres database is yours. You can pg_dump it, rsync it,
  back it up anywhere.
- CSV, JSON, OFX, QIF, and DB-dump export is a first-class feature in
  every edition including Community.
- We do not encrypt your data with a key only we hold. We do not lock
  you out of your own records.
- Even in post-termination read-only mode, export works.

This is the hero promise, codified.

---

## 11. Contact

- **Support:** `support@saebooks.com.au`
- **Licensing questions:** `licensing@saebooks.com.au`
- **Appeals:** in-portal "dispute this action" button or
  `appeals@saebooks.com.au`
- **Security disclosures:** `security@saebooks.com.au` (PGP key on the
  site)

---

*SPEC-LICENSING v1.0 — 2026-04-22. Companion to CHARTER.md §6.
Commercial terms are binding under the EULA / ToS; this document
describes how we implement them.*
