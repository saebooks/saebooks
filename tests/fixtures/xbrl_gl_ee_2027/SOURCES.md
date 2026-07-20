# XBRL GL EE 2027 (data-based KMD / "KMD3") schemas — provenance

Committed so the ``kmd_2027`` exporter test suite can XSD-validate generated
``EE0203001`` instances against the REAL e-MTA package, and golden-test against
EMTA's own worked example. Source package:
`~/records/saebooks/emta-schemas/andmepohine-kmd-2027/Andmepohine_KMD_2027/`
(see `andmepohine-kmd-2027-read.md` for the regime this feeds).

Provenance note: this schema set + the base-schema catalog below were built in a
parallel `feat/kmd3-2027` (`kmd_apa`) build and PORTED here onto the canonical
`kmd_2027` module. The two builds share the same XBRL GL taxonomy and the same
official sample; only the SAE-side module names differ (`kmd_apa` → `kmd_2027`).

| File | What it is |
|---|---|
| `gl/cor/gl-cor-2015-03-25.xsd` | XBRL GL core module (international) |
| `gl/bus/gl-bus-2015-03-25.xsd` | XBRL GL business module (international) |
| `gl/ext/gl-ext-2026-03-31.xsd` | Estonia extension module (`gl-ext`, APA-specific) |
| `gl/gen/gl-gen-2015-03-25.xsd` | XBRL GL general module (imported transitively) |
| `gl/plt/case-c-b-e/*.xsd` | The "case C+B+E" platform entry point — `gl-plt-2026-03-31.xsd` restricts COR+BUS+EXT to the actual allowed content model; this is the schema we validate against (`case-c-b-m-e` adds the MUC/multicurrency module, not used for `EE0203001`) |
| `sample.xml` | EMTA's own worked example, `English/XBRL_GL_sample_20260617.xml` — 20 `entryDetail` rows, our golden-test target |
| `sample_groups.xml` | EMTA's multi-source (`entrySourceId`) variant, `English/XBRL_GL_sample_20260617_groups.xml` — group-scenario reference only, not golden-tested here. Demonstrates `identifierCategory 300` (VAT-group member, incl. representative) paired with a second `identifierReference` (category 100) per `entryDetail`; the engine has no VAT-group data model to derive this from, so `kmd_2027` cannot produce it — documented as a known producibility-boundary gap in `generator.py`'s module docstring, same class as the `M_103`/`M_104` margin-scheme exclusion |

## Base XBRL 2003 schemas — fetched from xbrl.org, NOT in the EMTA package

`gl-cor-2015-03-25.xsd` imports `http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd`
by absolute URL; that in turn imports `xbrl-linkbase-2003-12-31.xsd`, which
imports `xl-2003-12-31.xsd` + `xlink-2003-12-31.xsd`. None of these four are
shipped in the EMTA zip (they're the generic XBRL 2.1 spec schemas every GL
taxonomy depends on) — fetched directly from `xbrl.org` (2026-07-11, verified
reachable) and committed here so validation works offline/sandboxed:

- `xbrl-instance-2003-12-31.xsd`
- `xbrl-linkbase-2003-12-31.xsd`
- `xl-2003-12-31.xsd`
- `xlink-2003-12-31.xsd`

`tests/services/lodgement/_xbrl_gl_validation.py`'s `_XBRL_BASE_CATALOG` maps
the four absolute URLs the vendor XSDs reference to these local files via an
`lxml.etree.Resolver`, so no network access is required (or attempted) at
validate time. The harness lives in the test tree (not the production
`kmd_2027` serializer) — the serializer stays a pure builder; only the tests
pull in `lxml.etree.XMLSchema`.

## Known defect in EMTA's own sample — READ

`sample.xml` line 154 (`<gl-cor:documentApplyToNumber>EA10001</gl-cor:documentApplyToNumber>`,
inside example 2, the partially-paid prepayment-invoice row) is **missing its
`contextRef="now"` attribute** — every other data element in the file carries
one, and the XSD's `documentApplyToNumberComplexType` requires it. Confirmed
by running our own validation harness against the schema set above: it is the
ONLY validation error the official sample produces (asserted directly in
`tests/services/lodgement/test_kmd_2027_schema_validation.py`). This is EMTA's
own authoring defect, not a divergence in our reading of the schema — our
generated instances always carry `contextRef="now"` on every element (see
`kmd_2027/serializer.py`) and are consequently valid where the official sample
is not, on this one element.

Note (port): the canonical `kmd_2027/serializer.py` originally REPRODUCED this
same defect — it emitted `documentApplyToNumber` with `context=False`, citing
the sample. The ported full-schema validation test caught it; the serializer was
corrected to carry `contextRef="now"` on that element too (the XSD requires it;
the sample is wrong). See the schema-validation test + the serializer's
`documentApplyToNumber` branch.
