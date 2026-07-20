"""REAL full-schema XSD validation harness for the 2027 data-based KMD
(XBRL GL, section ``EE0203001``) exporter output.

Ported from the parallel ``feat/kmd3-2027`` (``kmd_apa``) build. The canonical
``kmd_2027`` build's own ``mapping.py`` docstring previously asserted that full
XSD validation was impossible offline because ``gl-plt-2026-03-31.xsd`` imports
``http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd`` by absolute URL and
lxml raises ``XMLSchemaParseError`` when it cannot fetch it. That is true only
WITHOUT the four generic XBRL 2.1 base schemas — which the donor fetched from
``xbrl.org`` and committed under ``tests/fixtures/xbrl_gl_ee_2027/`` (see that
directory's ``SOURCES.md``). This module maps the four absolute URLs the vendor
XSDs reference to those local files via an ``lxml.etree.Resolver``, so a
generated ``EE0203001`` instance can be validated against the REAL taxonomy with
no network access.

Kept in the TEST tree, not in ``kmd_2027/serializer.py``: the serializer stays a
pure builder; only tests pull in ``lxml.etree.XMLSchema``.
"""
from __future__ import annotations

from pathlib import Path

from lxml import etree

FIXTURES_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "xbrl_gl_ee_2027"
)

# The four generic XBRL 2003 spec schemas the GL taxonomy imports by absolute
# URL — committed locally (see fixtures SOURCES.md) so validation is offline.
_XBRL_BASE_CATALOG = {
    "http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd": "xbrl-instance-2003-12-31.xsd",
    "http://www.xbrl.org/2003/xbrl-linkbase-2003-12-31.xsd": "xbrl-linkbase-2003-12-31.xsd",
    "http://www.xbrl.org/2003/xl-2003-12-31.xsd": "xl-2003-12-31.xsd",
    "http://www.xbrl.org/2003/xlink-2003-12-31.xsd": "xlink-2003-12-31.xsd",
}


class _LocalXbrlBaseResolver(etree.Resolver):
    def __init__(self, base_dir: Path) -> None:
        super().__init__()
        self._base_dir = base_dir

    def resolve(self, url: str, pubid: object, context: object):
        rel = _XBRL_BASE_CATALOG.get(url)
        if rel is None:
            return None
        return self.resolve_filename(str(self._base_dir / rel), context)


def load_gl_plt_schema(fixtures_dir: Path = FIXTURES_DIR) -> etree.XMLSchema:
    """Load the ``gl-plt-2026-03-31.xsd`` (case C+B+E) entry point, resolving
    the four xbrl.org base schemas locally."""
    parser = etree.XMLParser()
    parser.resolvers.add(_LocalXbrlBaseResolver(fixtures_dir))
    xsd_path = fixtures_dir / "gl" / "plt" / "case-c-b-e" / "gl-plt-2026-03-31.xsd"
    schema_doc = etree.parse(str(xsd_path), parser=parser)
    return etree.XMLSchema(schema_doc)


def validate_against_xsd(
    xml_bytes: bytes, fixtures_dir: Path = FIXTURES_DIR
) -> list[str]:
    """Validate an ``EE0203001`` instance against the real schema set. Returns
    a list of error strings (empty list = valid)."""
    schema = load_gl_plt_schema(fixtures_dir)
    doc = etree.fromstring(xml_bytes)
    if schema.validate(doc):
        return []
    return [str(e) for e in schema.error_log]


def validate_file_against_xsd(
    xml_path: Path, fixtures_dir: Path = FIXTURES_DIR
) -> list[str]:
    """Validate an on-disk instance (e.g. EMTA's official sample) against the
    real schema set. Returns a list of error strings (empty list = valid)."""
    schema = load_gl_plt_schema(fixtures_dir)
    doc = etree.parse(str(xml_path))
    if schema.validate(doc):
        return []
    return [str(e) for e in schema.error_log]
