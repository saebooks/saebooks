"""2027 data-based KMD (XBRL GL, section EE0203001) → element-name mapping.

CONFORMANCE STATUS — PINNED to the official package sample
----------------------------------------------------------
Every namespace URI, element qualified-name and enum token below is taken from
the real EMTA instance ``English/XBRL_GL_sample_20260617.xml`` (20 worked
transactions) shipped in ``Andmepohine_KMD_2027.zip`` and its taxonomy
(``gl-plt-2026-03-31.xsd``, entry point ``case-c-b-e`` = COR + BUS + EXT). This
is a far stronger pin than the box KMD ever had: a complete, real example
instance, not a hand-guessed shape.

Full XSD validation IS performed offline. The XBRL GL taxonomy imports the XBRL
2003 instance schema (``http://www.xbrl.org/2003/instance``) by absolute URL,
whose types (``tokenItemType`` …) are unresolvable — lxml raises
``XMLSchemaParseError`` on ``gl-plt-2026-03-31.xsd`` standalone — UNLESS the four
generic XBRL 2.1 base schemas it transitively imports are supplied locally. They
are: committed under ``tests/fixtures/xbrl_gl_ee_2027/`` and catalog-resolved by
``tests/services/lodgement/_xbrl_gl_validation.py`` (see that fixture dir's
``SOURCES.md``), so a generated ``EE0203001`` instance validates against the real
``case-c-b-e`` taxonomy with no network access — see
``tests/services/lodgement/test_kmd_2027_schema_validation.py``. (An earlier
build of this module asserted the STRUCTURAL story below was the best possible;
the ported base-schema catalog closed that gap.) The structural checks still
hold too: every emitted element / namespace is one the official sample uses,
``entryNumber`` = ``EE0203001``, ``accountSubType`` = ``KMDTYYP2026ap`` — see
``tests/services/lodgement/test_kmd_2027_serializer``.

READY FOR the 2027 data-based KMD; NOT "compliant with" (VTK-stage law).
"""
from __future__ import annotations

# --- Namespaces (SAMPLE header, verbatim) ------------------------------------
NS_XBRLI = "http://www.xbrl.org/2003/instance"
NS_XLINK = "http://www.w3.org/1999/xlink"
NS_LINK = "http://www.xbrl.org/2003/linkbase"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
NS_GL_COR = "http://www.xbrl.org/int/gl/cor/2015-03-25"
NS_GL_BUS = "http://www.xbrl.org/int/gl/bus/2015-03-25"
NS_GL_EXT = "https://xbrl.eesti.ee/gl/ext/2026-03-31"
NS_ISO639 = "http://www.xbrl.org/2005/iso639"
NS_ISO4217 = "http://www.xbrl.org/2003/iso4217"

# lxml nsmap for the root ``xbrli:xbrl`` element — prefixes match the sample so
# the serialized document reads identically to the official instance.
NSMAP = {
    "xbrli": NS_XBRLI,
    "xlink": NS_XLINK,
    "xbrll": NS_LINK,
    "xsi": NS_XSI,
    "gl-cor": NS_GL_COR,
    "gl-ext": NS_GL_EXT,
    "iso639": NS_ISO639,
    "iso4217": NS_ISO4217,
    "gl-bus": NS_GL_BUS,
}

# The taxonomy entry point (``xsi:schemaLocation`` + ``xbrll:schemaRef/@href``).
SCHEMA_NS = "https://xbrl.eesti.ee/gl/plt/2026-03-31"
SCHEMA_LOCATION_HREF = (
    "https://xbrl.eesti.ee/gl/ext/2026-03-31/gl/plt/case-c-b-e/gl-plt-2026-03-31.xsd"
)
SCHEMA_LOCATION = f"{SCHEMA_NS} {SCHEMA_LOCATION_HREF}"


def _q(ns: str, local: str) -> str:
    """Clark-notation qualified name for lxml element/attribute construction."""
    return f"{{{ns}}}{local}"


# --- Root / envelope element qualified names ---------------------------------
EL_XBRL = _q(NS_XBRLI, "xbrl")
EL_CONTEXT = _q(NS_XBRLI, "context")
EL_ENTITY = _q(NS_XBRLI, "entity")
EL_IDENTIFIER = _q(NS_XBRLI, "identifier")
EL_PERIOD = _q(NS_XBRLI, "period")
EL_INSTANT = _q(NS_XBRLI, "instant")
EL_UNIT = _q(NS_XBRLI, "unit")
EL_MEASURE = _q(NS_XBRLI, "measure")
EL_SCHEMA_REF = _q(NS_LINK, "schemaRef")

EL_ACCOUNTING_ENTRIES = _q(NS_GL_COR, "accountingEntries")
EL_DOCUMENT_INFO = _q(NS_GL_COR, "documentInfo")
EL_ENTRIES_TYPE = _q(NS_GL_COR, "entriesType")
EL_UNIQUE_ID = _q(NS_GL_COR, "uniqueID")
EL_LANGUAGE = _q(NS_GL_COR, "language")
EL_CREATION_DATE = _q(NS_GL_COR, "creationDate")
EL_ENTRIES_COMMENT = _q(NS_GL_COR, "entriesComment")
EL_PERIOD_COVERED_START = _q(NS_GL_COR, "periodCoveredStart")
EL_PERIOD_COVERED_END = _q(NS_GL_COR, "periodCoveredEnd")
EL_ENTITY_INFORMATION = _q(NS_GL_COR, "entityInformation")
EL_ENTRY_HEADER = _q(NS_GL_COR, "entryHeader")
EL_ENTRY_NUMBER = _q(NS_GL_COR, "entryNumber")

EL_BUS_CREATOR = _q(NS_GL_BUS, "creator")
EL_BUS_SOURCE_APPLICATION = _q(NS_GL_BUS, "sourceApplication")
EL_BUS_ORG_IDENTIFIERS = _q(NS_GL_BUS, "organizationIdentifiers")
EL_BUS_ORG_IDENTIFIER = _q(NS_GL_BUS, "organizationIdentifier")
EL_BUS_ORG_DESCRIPTION = _q(NS_GL_BUS, "organizationDescription")

EL_EXT_PERIOD_EXTRA_ID = _q(NS_GL_EXT, "periodExtraIdentifier")
EL_EXT_ENTRY_VERSION = _q(NS_GL_EXT, "entryVersion")
EL_EXT_ENTRY_SOURCE_ID = _q(NS_GL_EXT, "entrySourceId")
EL_EXT_ENTRY_SOURCE_COUNT = _q(NS_GL_EXT, "entrySourceCount")

# --- entryDetail (one per transaction) element qualified names ---------------
EL_ENTRY_DETAIL = _q(NS_GL_COR, "entryDetail")
EL_LINE_NUMBER_COUNTER = _q(NS_GL_COR, "lineNumberCounter")
EL_ACCOUNT = _q(NS_GL_COR, "account")
EL_ACCOUNT_SUB = _q(NS_GL_COR, "accountSub")
EL_ACCOUNT_SUB_ID = _q(NS_GL_COR, "accountSubID")
EL_ACCOUNT_SUB_TYPE = _q(NS_GL_COR, "accountSubType")
EL_SEGMENT_PARENT_TUPLE = _q(NS_GL_COR, "segmentParentTuple")
EL_PARENT_SUBACCOUNT_CODE = _q(NS_GL_COR, "parentSubaccountCode")
EL_PARENT_SUBACCOUNT_TYPE = _q(NS_GL_COR, "parentSubaccountType")
EL_AMOUNT = _q(NS_GL_COR, "amount")
EL_IDENTIFIER_REFERENCE = _q(NS_GL_COR, "identifierReference")
EL_IDENTIFIER_CODE = _q(NS_GL_COR, "identifierCode")
EL_IDENTIFIER_DESCRIPTION = _q(NS_GL_COR, "identifierDescription")
EL_IDENTIFIER_CATEGORY = _q(NS_GL_COR, "identifierCategory")
EL_DOCUMENT_NUMBER = _q(NS_GL_COR, "documentNumber")
EL_DOCUMENT_APPLY_TO_NUMBER = _q(NS_GL_COR, "documentApplyToNumber")
EL_DOCUMENT_DATE = _q(NS_GL_COR, "documentDate")
EL_TAXES = _q(NS_GL_COR, "taxes")
EL_TAX_PERCENTAGE_RATE = _q(NS_GL_COR, "taxPercentageRate")

EL_BUS_MEASURABLE = _q(NS_GL_BUS, "measurable")
EL_BUS_MEASURABLE_ID = _q(NS_GL_BUS, "measurableID")
EL_BUS_MEASURABLE_ID_SCHEMA = _q(NS_GL_BUS, "measurableIDSchema")
EL_BUS_MEASURABLE_QUANTITY = _q(NS_GL_BUS, "measurableQuantity")
EL_BUS_MEASURABLE_START_DATETIME = _q(NS_GL_BUS, "measurableStartDateTime")

# --- Attribute (unqualified) names -------------------------------------------
ATTR_CONTEXT_REF = "contextRef"
ATTR_UNIT_REF = "unitRef"
ATTR_DECIMALS = "decimals"
ATTR_ID = "id"
ATTR_SCHEME = "scheme"
ATTR_SCHEMA_LOCATION = _q(NS_XSI, "schemaLocation")
ATTR_XLINK_HREF = _q(NS_XLINK, "href")
ATTR_XLINK_ARCROLE = _q(NS_XLINK, "arcrole")
ATTR_XLINK_TYPE = _q(NS_XLINK, "type")

# --- Fixed tokens (SAMPLE, verbatim) -----------------------------------------
CONTEXT_ID = "now"                                # every fact's contextRef
UNIT_EUR = "EUR"
UNIT_PURE = "pureRef"
UNIT_NOT_USED = "NotUsed"
MEASURE_EUR = "iso4217:EUR"
MEASURE_PURE = "pure"

ENTRIES_TYPE = "other"                            # "the transaction type 'other' is always used"
ENTRY_NUMBER = "EE0203001"                        # VAT purchase/sale transactions and acts
ACCOUNT_SUB_TYPE_KMDTYYP = "KMDTYYP2026ap"        # classifier on accountSubID
DEFAULT_ENTRY_VERSION = "1.0"
DEFAULT_LANGUAGE = "iso639:et"
DEFAULT_ENTRIES_COMMENT = (
    "Data on VAT-related transactions and acts to the Tax and Customs Board"
)
SCHEMA_REF_ARCROLE = "http://www.w3.org/1999/xlink/properties/linkbase"
SCHEMA_REF_TYPE = "simple"

# gl-cor:amount / gl-bus:measurableQuantity carry 2 decimals; taxPercentageRate 3.
DECIMALS_AMOUNT = "2"
DECIMALS_RATE = "3"

# Country-role dimension (SAMPLE Example 8, intra-Community supply): the second
# accountSub carries the partner country (RTK2T2013ap) under a parent
# RIIGIROLL2022ap role (RR_OSTJA = buyer).
COUNTRY_CLASSIFIER = "RTK2T2013ap"
COUNTRY_ROLE_CLASSIFIER = "RIIGIROLL2022ap"
COUNTRY_ROLE_BUYER = "RR_OSTJA"

# gl-bus:measurable ID tokens.
MEASURABLE_INVOICE_TOTAL = "ARVE_KOGUSUMMA"       # total invoice amount ex-VAT
MEASURABLE_QUANTITY_SCHEMA = "MEASURABLEQUANTITY_enum"
MEASURABLE_ORIGINAL_INVOICE_DATE = "ALGSE_ARVE_KP"  # credit-invoice original date
MEASURABLE_EVENT_SCHEMA = "SYNDMUS2017ap"

# IDENTIFIERDESCRIPTION_enum tokens seen in the sample.
IDENT_DESC_REGCODE = "ARIREGISTRIKOOD"            # Estonian business registry code
IDENT_DESC_VAT_NUMBER = "KMKR_NUMBER"             # intra-EU partner VAT number
ORG_DESC_REGCODE = "ARIREGISTRIKOOD"

# IDENTIFIERCATEGORY_enum tokens (invoice specifics) seen in the sample.
IDENT_CAT_STANDARD = "100"                        # full partner + invoice identifier
IDENT_CAT_NATURAL_PERSON = "200"                  # natural person — no partner code
IDENT_CAT_UNDER_THRESHOLD = "103"                 # < €1,000 per partner — no partner code

# Bankruptcy-period marker (gl-ext:periodExtraIdentifier).
PERIOD_EXTRA_BANKRUPTCY = "PANKROTIPERIOOD"
