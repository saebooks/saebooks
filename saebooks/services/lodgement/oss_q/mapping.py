"""OSS-Q — company-side conventions + destination-country normalisation.

Pure/no-DB. See ``generator.py``'s module docstring for the compute shape
this feeds, and ``serializer.py``'s module docstring for the wire-format
STOP-AND-CONFIRM (deliberately NOT touched by this module).

Company-side ``TaxCode.reporting_type`` convention
----------------------------------------------------
``OSS_REPORTING_TYPE`` ("oss_eu_b2c") marks a company-side ``TaxCode`` row
as reporting under the Union OSS scheme — the company-side analogue of the
reference catalogue's ``OSS_EU`` marker code (``tax_codes.yaml``'s
OSS/IOSS header comment). A company provisions ONE such code per rate it
needs to distinguish:

  * ``TaxCode.rate == 0`` (the ordinary case — mirrors ``OSS_EU``'s
    ``rate_percent: 0.0000``, since the real VAT rate is the DESTINATION
    member state's, not a company-side constant) — the generator looks
    the destination rate up from ``oss_member_state_rates`` (reference
    DB, with an embedded fallback — see ``generator.py``).
  * ``TaxCode.rate > 0`` — an explicit per-line rate OVERRIDE (e.g. a
    company selling a reduced-rated good/service under OSS, where the
    reference table only carries each member state's STANDARD rate — see
    ``0011_oss_member_state_rates.py``'s scope note). The generator
    prefers this over the reference lookup whenever set.

This module does not validate that a company's OSS TaxCode carries this
exact string — ``TaxCode.reporting_type`` is unconstrained free text
everywhere else in the engine (no jurisdiction-wide whitelist exists;
confirmed against ``models/tax_code.py``), so "oss_eu_b2c" is a
documented convention this package's generator reads, not an enforced one.
"""
from __future__ import annotations

OSS_REPORTING_TYPE = "oss_eu_b2c"

# Destination member-state display names, keyed by ISO 3166-1 alpha-2 —
# the 17 states this build seeds a standard rate for: every
# _global/countries.yaml row with in_oss: true EXCEPT Estonia itself
# (see EE/oss_member_state_rates.yaml's header for which 8 other current
# EU member states are NOT yet covered — no countries.yaml row exists
# for them yet — and why). Kept in lock-step with that seed file's
# country_code column by inspection (same discipline as
# tax_return_generator._FALLBACK_BOX_DEFINITIONS).
MEMBER_STATE_NAMES: dict[str, str] = {
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "PT": "Portugal",
    "NL": "Netherlands",
    "BE": "Belgium",
    "LU": "Luxembourg",
    "AT": "Austria",
    "IE": "Ireland",
    "FI": "Finland",
    "SE": "Sweden",
    "DK": "Denmark",
    "PL": "Poland",
    "CZ": "Czechia",
    "LV": "Latvia",
    "LT": "Lithuania",
}

# alpha-3 -> alpha-2, for reference-DB rows keyed by countries.code
# (alpha-3, per Country/OssMemberStateRate) that need to resolve back to
# the alpha-2 codes this package's rows/tests key on.
_ALPHA3_TO_ALPHA2: dict[str, str] = {
    "DEU": "DE", "FRA": "FR", "ITA": "IT", "ESP": "ES", "PRT": "PT",
    "NLD": "NL", "BEL": "BE", "LUX": "LU", "AUT": "AT", "IRL": "IE",
    "FIN": "FI", "SWE": "SE", "DNK": "DK", "POL": "PL", "CZE": "CZ",
    "LVA": "LV", "LTU": "LT",
}
ALPHA2_TO_ALPHA3: dict[str, str] = {v: k for k, v in _ALPHA3_TO_ALPHA2.items()}


def alpha3_to_alpha2(code: str) -> str | None:
    """``"DEU"`` -> ``"DE"``; ``None`` for an unrecognised/unsupported code."""
    return _ALPHA3_TO_ALPHA2.get(code.upper())


# ``Contact.country`` (``models/contact.py``) is free text, defaulted
# "Australia" — an AU-centric legacy field with no ISO-code constraint
# anywhere in the schema. This alias table normalises the common text
# forms a filer is likely to have typed (English name, alpha-2, alpha-3)
# to the alpha-2 code above. Deliberately closed — an unrecognised string
# resolves to ``None`` (surfaced by the generator as a data-quality error,
# never silently guessed at) rather than attempting fuzzy matching.
_COUNTRY_TEXT_ALIASES: dict[str, str] = {}
for _code2, _name in MEMBER_STATE_NAMES.items():
    _code3 = ALPHA2_TO_ALPHA3[_code2]
    for _alias in (_name, _name.upper(), _name.lower(), _code2, _code2.lower(), _code3, _code3.lower()):
        _COUNTRY_TEXT_ALIASES[_alias] = _code2
del _code2, _name, _code3, _alias


def normalize_member_state(country_text: str | None) -> str | None:
    """Resolve a free-text ``Contact.country`` value to an alpha-2 OSS
    destination member-state code, or ``None`` if it does not match one
    of the 17 states this build recognises (see module header) — never
    guessed/fuzzy-matched."""
    if not country_text:
        return None
    return _COUNTRY_TEXT_ALIASES.get(country_text.strip())
