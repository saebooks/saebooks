# Peppol BIS Billing 3.0 — example instances + code lists — provenance

Committed so the ``services/einvoice`` serializer test suite has a REAL,
publisher-authored conformance reference to structurally diff against — not a
hand-typed guess at what a Peppol invoice looks like. Fetched 2026-07-11 from
the official `OpenPEPPOL/peppol-bis-invoice-3` repository (the source of the
published BIS Billing 3.0 specification, `docs.peppol.eu/poacc/billing/3.0/`):
`https://github.com/OpenPEPPOL/peppol-bis-invoice-3`, `master` branch.

## `examples/` — rule-illustration instances (`rules/examples/`)

| File | What it shows |
|---|---|
| `base-example.xml` | A full BIS 3.0 invoice — `CustomizationID`/`ProfileID` magic strings, `InvoiceTypeCode 380`, `EndpointID`/`PartyIdentification` ISO 6523 (EAS) scheme pattern, `PartyTaxScheme`/`PartyLegalEntity`, allowance/charge, `TaxTotal`/`TaxSubtotal`, `LegalMonetaryTotal`, two `InvoiceLine`s incl. a negative-quantity correction line. Category **S** (standard rate, 25%) |
| `Vat-category-S.xml` | Standard-rate (**S**) minimal worked example — `unitCode="C62"` (piece), used as the InvoicedQuantity unit-code source |
| `vat-category-E.xml` | **E** (exempt) — `cbc:TaxExemptionReasonCode` = `VATEX-EU-F` |
| `vat-category-Z.xml` | **Z** (zero-rated goods, domestic) |
| `vat-category-O.xml` | **O** (outside scope of tax) — free-text `cbc:TaxExemptionReason` = "Not subject to VAT" (no code — BT-121 and BT-120 are alternatives, not both required) |

No official AE/K/G/L/M example ships in this examples directory (only these
five rule-illustration files exist upstream) — `mapping.py`'s AE/K/G cells are
sourced from `codelist/UNCL5305.xml`'s own code descriptions +
`codelist/VATEX.xml`'s exemption-reason names, not from a worked instance.
Flagged, not silently assumed — see `mapping.py`'s own docstring for the
per-cell sourcing.

All five files verified **XSD-valid** against `tests/fixtures/ubl21/xsd/maindoc/
UBL-Invoice-2.1.xsd` (2026-07-11, `lxml.etree.XMLSchema.validate`).

## `codelist/` — the two code lists `mapping.py` is built from (`structure/codelist/`)

| File | What it is |
|---|---|
| `UNCL5305.xml` | BT-118 `cac:TaxCategory/cbc:ID` — the OpenPeppol-curated *subset* of UN/EDIFACT UNCL5305 that EN 16931 / BIS 3.0 actually use: `S / Z / E / AE / K / G / O / L / M` (+ `B`, Italy-only). This is the authoritative source for "there is no `AA` code" — the task brief's guess was wrong; corrected here against the real list |
| `VATEX.xml` | BT-121 `cbc:TaxExemptionReasonCode` — the VATEX code list (EU-wide `VATEX-EU-*` Directive-2006/112/EC article citations, plus a few member-state extensions e.g. `VATEX-FR-*`. No `VATEX-EE-*` entries exist — Estonia has no member-state VATEX extension registered) |
| `eas.xml` | ISO 6523 Electronic Address Scheme identifiers used in `cbc:EndpointID/@schemeID` and `cac:PartyIdentification/cbc:ID/@schemeID`. Confirms `0191` = "Centre of Registers and Information Systems of the Ministry of Justice" (Estonian registrikood / äriregistri kood) and `9931` = "Estonia VAT number" — cross-checked against the independently-fetched Peppol eDelivery Network code list (`docs.peppol.eu/edelivery/codelists/v9.7/…Participant identifier schemes…json`, same two rows, same day) |

## What this proves, and what it doesn't

Structural conformance proxy only (see `tests/fixtures/ubl21/SOURCES.md`'s
"What this does NOT validate" section) — these are real publisher examples,
diffed structurally (which BT-level elements exist, in what order, under
which parent), not a Schematron pass. `mapping.py` documents, cell by cell,
which UNCL5305/VATEX choice is (a) directly sourced from one of these five
example files, (b) sourced from the code list's own description text with no
worked example, or (c) this codebase's own inferred best-effort call — the
same "SPEC-VS-INFERRED" honesty convention `kmd_2027`'s and `ee_kmd3.py`'s own
module docstrings use.
