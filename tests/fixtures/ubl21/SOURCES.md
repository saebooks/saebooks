# UBL 2.1 Invoice schema set — provenance

Committed so the ``services/einvoice`` serializer test suite can XSD-validate
generated EN 16931 / Peppol BIS Billing 3.0 e-invoice XML against the REAL
OASIS UBL 2.1 schema, offline. Fetched 2026-07-11 from the OASIS committed
specification (the permanent, versioned artifact — not a draft mirror):
`https://docs.oasis-open.org/ubl/os-UBL-2.1/xsd/`.

| File | What it is |
|---|---|
| `xsd/maindoc/UBL-Invoice-2.1.xsd` | The Invoice document schema — root element `Invoice` |
| `xsd/common/UBL-CommonAggregateComponents-2.1.xsd` | ABIEs (Party, TaxCategory, InvoiceLine, …) |
| `xsd/common/UBL-CommonBasicComponents-2.1.xsd` | BBIEs (ID, Amount, Quantity, …) |
| `xsd/common/UBL-CommonExtensionComponents-2.1.xsd` | UBLExtensions wrapper (unused by our output, imported transitively) |
| `xsd/common/UBL-QualifiedDataTypes-2.1.xsd` / `UBL-UnqualifiedDataTypes-2.1.xsd` / `CCTS_CCT_SchemaModule-2.1.xsd` | UN/CEFACT Core Component Type datatype chain |
| `xsd/common/UBL-CoreComponentParameters-2.1.xsd` | Shared attribute groups (`currencyID`, `unitCode`, …) |
| `xsd/common/UBL-ExtensionContentDataType-2.1.xsd`, `UBL-CommonSignatureComponents-2.1.xsd`, `UBL-Signature*-2.1.xsd`, `UBL-XAdESv1*-2.1.xsd`, `UBL-xmldsig-core-schema-2.1.xsd` | Transitively imported via the extension-content chain even though our output carries no `UBLExtensions`/digital signature — present only so `lxml.etree.XMLSchema` can compile the full import graph offline |

## Relative layout preserved deliberately

`UBL-Invoice-2.1.xsd`'s own `xsd:import schemaLocation` values are
`../common/UBL-*-2.1.xsd` (relative to `maindoc/`), and the `common/` files
import each other by bare filename. The directory layout here
(`xsd/maindoc/` + `xsd/common/`) is kept identical to the OASIS zip's own
layout for exactly this reason: with it, `lxml.etree.XMLSchema(etree.parse(
"xsd/maindoc/UBL-Invoice-2.1.xsd"))` resolves every include **offline, with
no custom `Resolver`** (contrast the KMD3 XBRL GL schema set, which needed
one — see `tests/fixtures/xbrl_gl_ee_2027/SOURCES.md` — because that taxonomy
imports schemas by *absolute* `xbrl.org` URL; UBL 2.1's own import graph is
entirely relative, so a plain file-tree copy is sufficient here).

Verified compiling standalone (2026-07-11):
```
python3 -c "from lxml import etree; etree.XMLSchema(etree.parse('xsd/maindoc/UBL-Invoice-2.1.xsd'))"
```

## What this does NOT validate

XSD-valid UBL 2.1 is a necessary but not sufficient condition for EN 16931 /
Peppol BIS Billing 3.0 **conformance** — the semantic business rules (BR-*,
BR-CO-*, BR-S-*, BR-E-*, BR-AE-*, …) are expressed as compiled Schematron
(XSLT 2.0), which needs a Saxon/Java runtime lxml cannot execute. This fixture
set proves *structural* conformance (elements exist, are correctly typed,
correctly nested); `tests/fixtures/peppol_bis3/` (real OpenPeppol example
instances + the UNCL5305/VATEX codelists) is the structural conformance
*proxy* used instead — see that directory's own `SOURCES.md`.
