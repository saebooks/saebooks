# e-MTA statutory-format schemas — provenance

Committed here so the lodgement serializer test suite can validate generated
XML against the REAL e-MTA schemas (not the former PLACEHOLDER names). Fetched
from emta.ee's technical-information pages; full download set + README live at
`~/records/saebooks/emta-schemas/`.

| File | What it is | Used for |
|---|---|---|
| `tsd_schema_01.01.2025_eng.xsd` | Current (01.01.2025) Form TSD XSD — root `tsd_vorm` | lxml XSD-validation of generated TSD XML |
| `tsd_example.xml` | Official TSD example (`tsd_naide_xml_01.01.2025_eng.xml`) | parse-under-our-reader-assumptions check |
| `vatdeclaration.xsd` | KMD + KMD-INF XSD, root `vatDeclaration` | structural reference for KMD/KMD-INF |
| `vatdeclaration_example.xml` | Official KMD6 example (`vatdeclaration example.xml`) | KMD/KMD-INF element-name + order source of truth |

## Version caveat — READ

`vatdeclaration.xsd` in the download set is the **KMD5** vintage: its own
`<version>` annotation says "KMD4 until 12.2024, KMD5 from 01.2025", and its
`DeclarationBody` sequence has **no `transactions24`** element. The live KMD is
**KMD6** (24% standard rate, valid from taxable period 07.2025) — the example
XML (`vatdeclaration_example.xml`) IS KMD6 and does carry `transactions24`.

## TSD XSD repair — READ

The official `tsd_schema_01.01.2025_eng.xsd` download was truncated at two
`<xs:documentation>` strings (their closing `</xs:documentation>` tags were lost
mid-text), making the file not well-formed XML — lxml could not even load it.
The committed copy reinserts those two closing tags (informational annotation
text only; NO schema-semantic change — the repaired XSD compiles and validates
both the official `tsd_example.xml` and our generated TSD output). This is the
one edit to a downloaded artifact in this directory.

## KMD version caveat

Consequence: a KMD6 document (which ours is — 2026 periods, 24% rate) will NOT
validate against this KMD5 XSD (the `transactions24` element is `unexpected`).
No KMD6 `vatdeclaration.xsd` is published in the set (the `xsd.zip` in the
download set is an unrelated customs-declaration bundle — verified). So the KMD
validation test asserts **structural** conformance against the KMD6 example +
this XSD's shared type definitions, and is explicitly documented as NOT a full
XSD validation. The TSD XSD, by contrast, IS current (01.01.2025) and the
generated TSD XML validates against it in full.
