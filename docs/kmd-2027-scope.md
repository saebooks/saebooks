# KMD 2027 (data-based KMD / "KMD3") — producibility scope

Status snapshot for `saebooks/services/lodgement/kmd_2027` against EMTA's own
worked example (`tests/fixtures/xbrl_gl_ee_2027/sample.xml`, 20 numbered
`entryDetail` rows). Golden test:
`tests/services/lodgement/test_kmd_2027_apa_golden.py`.

READY FOR the 2027 data-based KMD; NOT "compliant with" (VTK-stage law) — see
`generator.py`'s module docstring for the full list of documented, valid
divergences (partner-code category, country subaccount, credit-note original
date, bill-side invoice total).

## Reproduction scoreboard: 15 produced + 5 no-mapping = 20

**Packet 3 closed the two rows that were previously a model gap:**

* **Row 2 — prepayment `documentApplyToNumber`.** Represented as a field-swap
  on the invoice's own `M_101` row: a `POSTED` `INCOMING` `Payment` allocated
  to the invoice (`PaymentAllocation.invoice_id`, data the engine already
  had) whose `payment_date` precedes the invoice's own `issue_date`. No
  separate payment-derived row (that would double-count the turnover the
  invoice's own row already reports); no schema change. See `generator.py`'s
  `_prepayment_invoice_ids` + its module docstring for the full rationale,
  including why the trigger cannot misfire on an ordinary late-paid invoice.
* **Row 19 — purchase-side credit note.** `SupplierCreditNote` (the
  purchase-side mirror of `CreditNote`, already modelled — `models/
  supplier_credit_note.py`) now feeds the KMD 2027 purchase side through the
  SAME `(reporting_type, role)` resolution as an ordinary bill, at
  `sign=-1` — no new KMDTYYP leaf, matching how the sale side already signs
  customer credit notes.

## The 5 leaves with NO engine mapping (`engine: []` in `kmdtyyp_mapping.yaml`)

These are asserted ABSENT from every generated period — never guessed, never
silently coded to the wrong leaf. Quoted directly from the seed
(`saebooks/seeds/jurisdictions/EE/kmdtyyp_mapping.yaml`), which is the single
source of truth for this list:

### M_103 — Margin scheme, resale of second-hand goods / art / collectors' items / antiques

> TIER 3 — margin scheme (KMS §41/§42). The reported taxable value is the
> MARGIN (sale price − acquisition cost), not the line net. An existing
> `margin_scheme` tag exists (AU MGN) but mapping it would report the full
> value as the margin — WRONG. Needs margin accounting (per-line
> acquisition-cost tracking + margin base).
> Future: `(margin_resale, sale) -> M_103`.

**What building it would take:** a margin-accounting feature — per-line
acquisition-cost capture on the sale side (today's `InvoiceLine` has no
"cost of the specific second-hand item sold" field), a `margin_resale`
`reporting_type`, and generator logic computing `taxable_value = sale_price −
acquisition_cost` instead of the line net. This is a genuine new feature, not
a mapping gap.

### M_104 — Margin scheme, travel agencies

> TIER 3 — travel-agency margin scheme (KMS §40). Same margin-accounting
> feature as M_103 (report margin, not value), distinct leaf.
> Future: `(margin_travel, sale) -> M_104`.

**What building it would take:** the same margin-accounting feature as
M_103, applied to travel-agency supplies specifically (a distinct
`reporting_type` so the two margin schemes stay separately reportable).

### M_206 — Intra-Community supply of goods, excise goods

> TIER 2 — classifier sub-case of intra-Community goods supply.
>
> `engine_pending`: `(zero_ic_excise, sale)`, `pending_engine_emit: true` —
> "Seed EE tax_code (rate 0%, direction sale, reporting_type=zero_ic_excise);
> no generator wiring (sale role already dispatched). Then move this tuple
> into `engine:`."

**What building it would take:** lighter than M_103/M_104 — this is a TIER 2
gap, not TIER 3. The `sale` role dispatch path in `generator.py` already
handles any `reporting_type` uniformly (no excise-specific code needed); the
gap is purely seed-side — provision an EE `zero_ic_excise` tax code (0%,
sale direction) so a company can actually tag an excise-goods IC supply line,
then move the `kmdtyyp_mapping.yaml` entry from `engine_pending` to `engine`.
No generator code changes.

### M_210 — Other goods taxable at 0%, incl. advance payment

> TIER 2 — 'other' 0% goods bucket (KMS §15(3)/(4) not already an IC-supply /
> export / traveller leaf).
>
> `engine_pending`: `(zero_other_goods, sale)`, `pending_engine_emit: true` —
> "Seed EE tax_code (rate 0%, direction sale, reporting_type=zero_other_goods);
> no generator wiring. Then move this tuple into `engine:`."

**What building it would take:** same shape as M_206 — seed a
`zero_other_goods` EE tax code, then promote the mapping entry. No generator
code changes (the `sale` role path is already generic).

### O_601 — Reduction/increase of input VAT as a result of input VAT correction

> TIER 3 as a distinct leaf — ordinary credit adjustments ride the base
> input leaf (O_101) as a SIGNED row (sample Example 19 = O_101 at −240), so
> this is not a functional gap for the common case; only a standalone
> §29(9)/§32 correction posting would need O_601. Needs a distinct
> input-VAT correction/adjustment posting type (signed: '+' on reduction, '-'
> on increase). Future: `(input_correction, input) -> O_601`.

**What Packet 3 confirms:** the "common case" this note describes — an
ordinary purchase-side credit note reducing input VAT — is now produced,
correctly, as a signed `O_101` row (row 19). `O_601` itself remains
unmapped: it is reserved for a *standalone* VAT correction/adjustment that is
NOT tied to a credit note or bill — e.g. a bad-debt input-VAT clawback, an
apportionment true-up, or a §29(9)/§32 correction entered directly. **What
building it would take:** a new record type or journal-entry-adjacent
posting flow the engine does not have today (this is deliberately NOT a
manual-journal-entry workaround — see the engine's `NEVER post manual
journal entries` rule), a new `input_correction` `reporting_type`, and a
signed-amount input role in `generator.py`'s `_emit_purchase` (already
sign-aware after Packet 3, so the generator-side lift is now small — the
gap is the missing record type upstream of it).

## Where this leaves the exporter

17 rows on the Packet-3 golden period (15 produced examples + 2 documented
reverse-charge fan-out "other half" extras — `S_101`/`O_401` rows the sample
itself expects two independent Bills to reproduce). Every unmapped leaf above
is a genuine future feature (margin accounting, two seed-only 0%-goods
sub-cases, or a standalone VAT-correction record type) — never a silently
wrong classification. See `generator.py`'s module docstring for the full
mapped-leaf producibility-boundary register (identifier category, country
subaccount, credit-note original date, bill-side invoice total) alongside
this one.
