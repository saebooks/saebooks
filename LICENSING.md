# SAE Books — Licensing at a Glance

> *Your books. Your database. Your control.*
>
> This document is the one-page summary. Customer-facing detail lives in
> `SPEC-LICENSING.md`. The legal text of the public licence lives in
> `LICENSE`. Trademark policy lives in `TRADEMARK.md`. Contributor terms
> live in `CLA.md`. Strategic decisions and the edition matrix live in
> `CHARTER.md`. If anything in this summary conflicts with one of those
> documents, the more specific document wins.

---

## What licence is the code under?

SAE Books is **dual-licensed**:

1. **AGPL-3.0** — the public, open-source licence. You can read, modify,
   self-host, and redistribute the source under AGPL-3.0's terms. The
   network-copyleft clause means that if you run a modified version as
   a service, you must publish your modifications under AGPL.

2. **Commercial licence** — a paid, separate agreement for customers
   who want to use SAE Books *without* AGPL's obligations. Typical
   buyers: integrators bundling SAE Books into a proprietary product,
   organisations whose policy or contractual position rules out AGPL
   code, or businesses on the Business / Pro / Enterprise subscription
   tiers (the subscription includes a commercial-use grant).

Both licences cover the same code. You pick which one applies to your
deployment based on what you're doing with it.

## Which repositories does this cover?

| Repository | Licence | Notes |
|---|---|---|
| `saebooks` (engine — FastAPI, DB, business logic) | AGPL-3.0 + commercial | Charter §5 |
| `saebooks-web` (server-rendered frontend — FastAPI + Jinja + HTMX) | AGPL-3.0 + commercial | Same posture as engine |
| `saebooks-l10n-*` (localisation packs — country CoA, tax codes, report templates) | AGPL-3.0 | Community-maintainable |
| Certified e-lodgement engines (ATO SBR, HMRC MTD, IRS e-file, etc.) | Commercial only | Funded by paid tiers (Pro / Enterprise); see Charter §6.4–§6.5 |
| Hosted SaaS infrastructure (billing portal, onboarding, ops) | Proprietary, never published | Not part of the public source tree |
| First-party premium plugins | Per-plugin (commercial unless otherwise marked) | See `saebooks.toml` manifest |

## What about my fork? Can I sell it?

You can fork SAE Books and run it commercially under AGPL-3.0 — that's
the deal AGPL gives you. Two things you cannot do:

1. **Run a modified fork as a service without publishing your changes.**
   AGPL's network-copyleft applies. If you patch SAE Books and offer it
   to users over a network, your patches must be available under AGPL.
2. **Call your fork "SAE Books" or use the SAE Books logo.** That's a
   trademark restriction, separate from the source-code licence. Fork
   the code, just not the brand. See `TRADEMARK.md`.

If you want to bundle SAE Books inside a proprietary product without
AGPL applying, talk to us about a commercial licence:
`licensing@saebooks.com.au`.

## What about contributions?

Every external contributor signs the SAE Books CLA before any pull
request can be merged. The CLA grants SAE Engineering the right to
distribute the contribution under AGPL *and* under our commercial
licence. This keeps the dual-licensing model viable without
re-contacting every contributor whenever a customer buys a commercial
licence.

The CLA is modelled on the Apache Individual Contributor License
Agreement (ICLA). Full text lives in `CLA.md`.

## Self-compiling and feature flags

SAE Books is open core. Features that belong to the paid editions
(see `CHARTER.md` §6 and §12.1) are gated by runtime flags. Under
AGPL, you have the right to flip those flags in a self-compiled
build. **That is not a licence violation.**

What is a licence violation: running a SAE-distributed binary with a
tampered, expired, or revoked licence key in a production deployment
that requires a commercial licence (Offline, Business, Pro,
Enterprise).

The practical reason most customers buy a licence anyway is the
moat: signed releases, LTS branches, support, certified e-lodgement
plumbing, and the ecosystem. The DRM is intentionally weak; the
support story is the reason the maths works.

## What about my data?

Your data is yours, regardless of licence. AGPL, commercial, expired
subscription, terminated for cause — you can always export it (CSV,
JSON, OFX, QIF, full DB dump). See `CHARTER.md` §6.14 and
`SPEC-LICENSING.md` §10.

## Where does each topic live?

- **Why the project exists, the editions, the feature matrix:**
  `CHARTER.md`
- **End-customer licence walkthrough (USB binding, transfers,
  subscription mechanics, AUP):** `SPEC-LICENSING.md`
- **Pricing:** `SPEC-PRICING.md` (private)
- **Public licence legal text:** `LICENSE` (AGPL-3.0)
- **Brand and name protection:** `TRADEMARK.md`
- **Contributor terms:** `CLA.md`
- **Acceptable Use Policy:** `CHARTER.md` §6.12 and the EULA / ToS
  (separate document)

## Contact

- **Licensing questions / commercial licence purchases:**
  `licensing@saebooks.com.au`
- **Trademark questions:** `trademark@saebooks.com.au`
- **Security disclosures:** `security@saebooks.com.au`
- **General support:** `support@saebooks.com.au`

---

*LICENSING v1.0 — 2026-04-26.*
