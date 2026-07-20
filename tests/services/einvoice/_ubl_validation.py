"""Real UBL 2.1 XSD validation harness for ``services.einvoice`` output.

Unlike the KMD3 XBRL GL taxonomy (``tests/services/lodgement/
_xbrl_gl_validation.py``), UBL 2.1's own import graph is entirely RELATIVE
(``../common/UBL-*.xsd``, bare-filename imports within ``common/``) — see
``tests/fixtures/ubl21/SOURCES.md``. No custom ``etree.Resolver`` is needed;
``lxml.etree.XMLSchema`` resolves the whole tree offline from a plain file-tree
copy. Kept in the TEST tree, not in ``serializer.py`` — the serializer stays a
pure builder; only tests pull in ``lxml.etree.XMLSchema``.
"""
from __future__ import annotations

from pathlib import Path

from lxml import etree

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "ubl21"
_INVOICE_XSD = FIXTURES_DIR / "xsd" / "maindoc" / "UBL-Invoice-2.1.xsd"

_schema: etree.XMLSchema | None = None


def _get_schema() -> etree.XMLSchema:
    global _schema
    if _schema is None:
        _schema = etree.XMLSchema(etree.parse(str(_INVOICE_XSD)))
    return _schema


def validate_ubl_invoice(xml_bytes: bytes) -> None:
    """Raise ``AssertionError`` with the full lxml error log if ``xml_bytes``
    is not a valid ``UBL-Invoice-2.1.xsd`` instance."""
    schema = _get_schema()
    doc = etree.fromstring(xml_bytes)
    if not schema.validate(doc):
        errors = "\n".join(str(e) for e in schema.error_log)
        raise AssertionError(f"UBL 2.1 Invoice schema validation failed:\n{errors}")
